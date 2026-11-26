#!/usr/bin/env python3
"""Linux TCP echo worker used by the FD/EMFILE drill API.

The process intentionally lowers RLIMIT_NOFILE, keeps one reserved descriptor,
and opens sample log files until its descriptor table is full.  A full table
makes a real accept(2) call fail with EMFILE.  The accept thread then uses its
reserved descriptor to accept and immediately close the one probe connection,
giving the client EOF/RST promptly without a polling busy loop.

The only control channel is line-delimited JSON on stdin/stdout.  All logs go
to stderr so stdout remains a protocol stream.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import resource
import select
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EMFILE = errno.EMFILE


def log(message: str) -> None:
    print(f"[echo-worker pid={os.getpid()}] {message}", file=sys.stderr, flush=True)


def lower_nofile_limit(limit: int) -> dict[str, int]:
    """Lower this process's NOFILE hard and soft limits."""
    soft_before, hard_before = resource.getrlimit(resource.RLIMIT_NOFILE)

    if limit < 8:
        raise ValueError("nofile limit must be at least 8")

    if hard_before == resource.RLIM_INFINITY:
        hard_after = limit
    else:
        hard_after = min(int(hard_before), limit)
    soft_after = min(limit, hard_after)

    resource.setrlimit(resource.RLIMIT_NOFILE, (soft_after, hard_after))
    soft_now, hard_now = resource.getrlimit(resource.RLIMIT_NOFILE)
    return {
        "requested": limit,
        "soft_before": int(soft_before),
        "hard_before": int(hard_before),
        "soft": int(soft_now),
        "hard": int(hard_now),
    }


def count_open_fds() -> tuple[int, str | None]:
    """Return the number of open descriptors.

    Listing /proc/self/fd itself needs a descriptor.  If that fails with
    EMFILE, the table is full and its capacity is the soft RLIMIT_NOFILE.
    """
    try:
        return len(os.listdir("/proc/self/fd")), None
    except OSError as exc:
        if exc.errno == EMFILE:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            return int(soft), "procfs listing returned EMFILE; table is full"
        return -1, f"procfs listing failed with errno={exc.errno}"


@dataclass
class Metrics:
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_at: float = field(default_factory=time.time)
    emfile_total: int = 0
    accepted_total: int = 0
    reaped_after_emfile_total: int = 0
    recovery_accept_emfile_total: int = 0
    active_connections: int = 0
    echo_bytes_total: int = 0
    echo_chunks_total: int = 0
    last_emfile_at: float | None = None
    last_emfile_errno: int | None = None
    last_recovery_error: str | None = None
    other_accept_errors_total: int = 0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "started_at_unix": self.started_at,
                "uptime_seconds": round(time.time() - self.started_at, 3),
                "emfile_total": self.emfile_total,
                "accepted_total": self.accepted_total,
                "reaped_after_emfile_total": self.reaped_after_emfile_total,
                "recovery_accept_emfile_total": self.recovery_accept_emfile_total,
                "active_connections": self.active_connections,
                "echo_bytes_total": self.echo_bytes_total,
                "echo_chunks_total": self.echo_chunks_total,
                "last_emfile_at_unix": self.last_emfile_at,
                "seconds_since_last_emfile": (
                    None
                    if self.last_emfile_at is None
                    else round(time.time() - self.last_emfile_at, 6)
                ),
                "last_emfile_errno": self.last_emfile_errno,
                "last_recovery_error": self.last_recovery_error,
                "other_accept_errors_total": self.other_accept_errors_total,
            }


class EchoWorker:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sample_dir: Path,
        backlog: int = 32,
        sample_content_bytes: int = 512,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.sample_dir = sample_dir
        self.backlog = backlog
        self.sample_content_bytes = sample_content_bytes

        self.metrics = Metrics()
        self.stop_event = threading.Event()
        self.fd_lock = threading.Lock()
        self.tid_ready = threading.Event()
        self.accept_thread_tid: int | None = None

        self.server: socket.socket | None = None
        self.bound_port: int | None = None
        self.rescue_fd: int | None = None

        self.sample_handles: list[Any] = []
        self.sample_records: list[dict[str, Any]] = []
        self.fill_error: dict[str, Any] | None = None

    def start_socket(self) -> dict[str, Any]:
        self.sample_dir.mkdir(parents=True, exist_ok=True)

        server = socket.socket(socket.AF_INET6 if ":" in self.host else socket.AF_INET,
                               socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Dual-stack when binding "::"; harmless for the default 0.0.0.0.
        if server.family == socket.AF_INET6:
            try:
                server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        server.bind((self.host, self.requested_port))
        server.listen(self.backlog)
        server.setblocking(False)
        self.server = server
        self.bound_port = server.getsockname()[1]

        # This descriptor both proves that the table is full and provides the
        # one slot used to accept and close an EMFILE probe.
        self.rescue_fd = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)

        thread = threading.Thread(
            target=self._accept_loop, name="fd-drill-accept", daemon=True
        )
        thread.start()
        if not self.tid_ready.wait(timeout=2.0):
            raise RuntimeError("accept thread did not report its thread id")

        return {
            "host": self.host,
            "port": self.bound_port,
            "listener_fd": server.fileno(),
            "rescue_fd": self.rescue_fd,
            "accept_thread_tid": self.accept_thread_tid,
        }

    def close(self) -> None:
        self.stop_event.set()
        try:
            if self.server is not None:
                self.server.close()
        finally:
            self._close_samples()
            if self.rescue_fd is not None:
                try:
                    os.close(self.rescue_fd)
                except OSError:
                    pass
                self.rescue_fd = None

    # ---------- sample log descriptors ----------

    def _write_sample_file(self, index: int, size_bytes: int) -> Path:
        # A nanosecond stamp keeps repeated exhaust/release cycles on distinct
        # paths without deleting log evidence from the mounted directory.
        batch = f"{time.time_ns()}-{os.getpid()}"
        name = f"sample-{batch}-{index:04d}.log"
        path = self.sample_dir / name
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} sample logger "
            f"batch={batch} index={index} "
        ).encode()
        payload = line + (b"x" * max(0, size_bytes - len(line) - 1)) + b"\n"
        if size_bytes >= len(line) + 1:
            path.write_bytes(payload)
        else:
            path.write_bytes(line[: max(0, size_bytes)])
        return path

    def _open_one_sample(self, *, size_bytes: int) -> dict[str, Any]:
        index = len(self.sample_handles) + 1
        path = self._write_sample_file(index, size_bytes)
        # Serialize descriptor allocation with EMFILE rescue handling.
        with self.fd_lock:
            handle = open(path, "r+", encoding="utf-8", errors="replace")
            fd = handle.fileno()
            self.sample_handles.append(handle)
            record = {
                "index": index,
                "path": str(path),
                "fd": fd,
                "size_bytes": path.stat().st_size,
            }
            self.sample_records.append(record)
            return record

    def open_samples(
        self,
        *,
        count: int | None = None,
        fill_to_limit: bool = False,
        file_size_bytes: int | None = None,
    ) -> dict[str, Any]:
        size = (
            self.sample_content_bytes
            if file_size_bytes is None
            else max(0, int(file_size_bytes))
        )
        opened: list[dict[str, Any]] = []
        self.fill_error = None

        if count is not None:
            for _ in range(max(0, int(count))):
                try:
                    opened.append(self._open_one_sample(size_bytes=size))
                except OSError as exc:
                    self.fill_error = {
                        "operation": "open",
                        "errno": exc.errno,
                        "error": exc.strerror,
                    }
                    if exc.errno != EMFILE:
                        raise
                    break

        if fill_to_limit:
            # RLIMIT_NOFILE bounds descriptor numbers/count; this loop cannot
            # usefully exceed it and the extra cap guards against odd limits.
            soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            safety_cap = int(soft_limit) + 8
            while len(self.sample_handles) < safety_cap:
                try:
                    opened.append(self._open_one_sample(size_bytes=size))
                except OSError as exc:
                    self.fill_error = {
                        "operation": "open",
                        "errno": exc.errno,
                        "error": exc.strerror,
                    }
                    if exc.errno != EMFILE:
                        raise
                    break

        fd_count, fd_note = count_open_fds()
        return {
            "opened_this_call": len(opened),
            "open_sample_files": len(self.sample_handles),
            "sample_files": list(self.sample_records),
            "fill_error": self.fill_error,
            "open_fd_count": fd_count,
            "fd_count_note": fd_note,
            "fd_table_full": fd_note is not None and "EMFILE" in fd_note,
        }

    def _close_samples(self) -> dict[str, Any]:
        closed = len(self.sample_handles)
        errors: list[str] = []
        with self.fd_lock:
            for handle in self.sample_handles:
                try:
                    handle.close()
                except OSError as exc:
                    errors.append(str(exc))
            self.sample_handles.clear()
            records = self.sample_records
            self.sample_records = []
            self.fill_error = None
        return {"closed_sample_files": closed, "errors": errors, "closed_paths": [r["path"] for r in records]}

    def release_samples(self) -> dict[str, Any]:
        result = self._close_samples()
        fd_count, note = count_open_fds()
        result.update(
            {
                "open_sample_files": 0,
                "open_fd_count": fd_count,
                "fd_count_note": note,
                "listener_fd": self.server.fileno() if self.server is not None else None,
                "listener_accepting": self.server is not None,
            }
        )
        return result

    # ---------- TCP echo and accept handling ----------

    def _serve_echo(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with self.metrics.lock:
            self.metrics.active_connections += 1
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while not self.stop_event.is_set():
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    conn.sendall(chunk)
                except OSError:
                    break
                with self.metrics.lock:
                    self.metrics.echo_bytes_total += len(chunk)
                    self.metrics.echo_chunks_total += 1
        finally:
            with self.metrics.lock:
                self.metrics.active_connections -= 1
            try:
                conn.close()
            except OSError:
                pass

    def _recover_from_emfile(self) -> str:
        """Use the reserved slot to reap and close one waiting probe.

        Returns "reaped", "empty", or "emfile".  The caller returns to
        select(); a pathological "emfile" recovery also backs off briefly so a
        lost rescue descriptor can never create a tight retry loop.
        """
        assert self.server is not None
        outcome = "empty"
        with self.fd_lock:
            if self.rescue_fd is not None:
                try:
                    os.close(self.rescue_fd)
                except OSError:
                    pass
                self.rescue_fd = None

            try:
                try:
                    conn, _addr = self.server.accept()
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        outcome = "empty"
                    elif exc.errno == EMFILE:
                        outcome = "emfile"
                        with self.metrics.lock:
                            # A real recovery accept(2) EMFILE is still counted.
                            self.metrics.emfile_total += 1
                            self.metrics.recovery_accept_emfile_total += 1
                            self.metrics.last_emfile_at = time.time()
                            self.metrics.last_emfile_errno = exc.errno
                            self.metrics.last_recovery_error = (
                                "accept with rescue slot returned EMFILE"
                            )
                    else:
                        raise
                else:
                    outcome = "reaped"
                    with self.metrics.lock:
                        self.metrics.reaped_after_emfile_total += 1
                    try:
                        # No read/echo: a descriptor is unavailable for a normal
                        # long-lived session.  A plain close produces EOF; unread
                        # probe data can make Linux send RST, also accepted.
                        conn.close()
                    except OSError:
                        pass
            finally:
                if self.rescue_fd is None:
                    try:
                        self.rescue_fd = os.open(
                            os.devnull, os.O_RDONLY | os.O_CLOEXEC
                        )
                    except OSError as exc:
                        outcome = "emfile"
                        with self.metrics.lock:
                            self.metrics.last_recovery_error = (
                                f"could not recreate rescue fd: {exc}"
                            )
        return outcome

    def _accept_loop(self) -> None:
        self.accept_thread_tid = threading.get_native_id()
        self.tid_ready.set()
        assert self.server is not None

        while not self.stop_event.is_set():
            try:
                readable, _, _ = select.select([self.server], [], [], 0.25)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if self.stop_event.is_set():
                    break
                with self.metrics.lock:
                    self.metrics.other_accept_errors_total += 1
                    self.metrics.last_recovery_error = f"select: {exc}"
                # An unexpected persistent error must not busy-spin.
                self.stop_event.wait(0.25)
                continue

            if not readable:
                continue

            while True:
                try:
                    conn, addr = self.server.accept()
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    if exc.errno == EMFILE:
                        with self.metrics.lock:
                            self.metrics.emfile_total += 1
                            self.metrics.last_emfile_at = time.time()
                            self.metrics.last_emfile_errno = exc.errno
                        recovery = self._recover_from_emfile()
                        # Return to select() instead of immediately calling
                        # accept() again.  With the table full, Linux can return
                        # EMFILE before discovering that the queue is empty; an
                        # immediate retry would therefore spin.  select() sleeps
                        # until another connection is actually pending.
                        if recovery == "emfile":
                            # Defensive backoff if the reserved slot could not
                            # be restored; the normal path does not wait here.
                            self.stop_event.wait(0.05)
                        break

                    if self.stop_event.is_set():
                        break
                    with self.metrics.lock:
                        self.metrics.other_accept_errors_total += 1
                        self.metrics.last_recovery_error = f"accept: {exc}"
                    self.stop_event.wait(0.1)
                    break

                with self.metrics.lock:
                    self.metrics.accepted_total += 1
                thread = threading.Thread(
                    target=self._serve_echo,
                    args=(conn, addr),
                    name=f"echo-{addr[0]}:{addr[1]}",
                    daemon=True,
                )
                thread.start()

    # ---------- control plane ----------

    def status(self, rlimit_info: dict[str, int]) -> dict[str, Any]:
        fd_count, note = count_open_fds()
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        with self.fd_lock:
            sample_count = len(self.sample_handles)
            records = list(self.sample_records[-20:])
            fill_error = self.fill_error
            rescue_fd = self.rescue_fd
        return {
            "ok": True,
            "type": "status",
            "pid": os.getpid(),
            "rlimit_nofile": {
                "soft": int(soft),
                "hard": int(hard),
                "startup": rlimit_info,
            },
            "listener": {
                "host": self.host,
                "port": self.bound_port,
                "requested_port": self.requested_port,
                "backlog": self.backlog,
                "fd": self.server.fileno() if self.server is not None else None,
            },
            "rescue_fd": rescue_fd,
            "accept_thread_tid": self.accept_thread_tid,
            "sample_dir": str(self.sample_dir),
            "open_sample_files": sample_count,
            "sample_files_tail": records,
            "fill_error": fill_error,
            "open_fd_count": fd_count,
            "fd_count_note": note,
            "fd_table_full": note is not None and "EMFILE" in note,
            "metrics": self.metrics.snapshot(),
        }


def emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FD exhaustion TCP echo worker")
    parser.add_argument("--host", default=os.getenv("ECHO_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ECHO_PORT", "9000")))
    parser.add_argument(
        "--nofile-limit",
        type=int,
        default=int(os.getenv("DRILL_NOFILE_LIMIT", "48")),
    )
    parser.add_argument(
        "--sample-dir",
        type=Path,
        default=Path(os.getenv("DRILL_SAMPLE_DIR", "/tmp/fd-emfile-drill-samples")),
    )
    parser.add_argument("--backlog", type=int, default=32)
    args = parser.parse_args(argv)

    rlimit_info = lower_nofile_limit(args.nofile_limit)
    worker = EchoWorker(
        host=args.host,
        port=args.port,
        sample_dir=args.sample_dir,
        backlog=args.backlog,
    )
    socket_info = worker.start_socket()
    emit(
        {
            "ok": True,
            "type": "ready",
            "pid": os.getpid(),
            "rlimit_nofile": rlimit_info,
            **socket_info,
        }
    )
    log(f"listening on {args.host}:{worker.bound_port}, RLIMIT_NOFILE={rlimit_info}")

    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            command = request.get("cmd")
            if command == "ping":
                result: dict[str, Any] = {"ok": True, "type": "pong"}
            elif command == "status":
                result = worker.status(rlimit_info)
            elif command == "open_samples":
                result = {
                    "ok": True,
                    "type": "open_samples",
                    **worker.open_samples(
                        count=request.get("count"),
                        fill_to_limit=bool(request.get("fill_to_limit", False)),
                        file_size_bytes=request.get("file_size_bytes"),
                    ),
                }
            elif command == "exhaust":
                result = {
                    "ok": True,
                    "type": "exhaust",
                    **worker.open_samples(
                        fill_to_limit=True,
                        file_size_bytes=request.get("file_size_bytes"),
                    ),
                }
            elif command == "release_samples":
                result = {
                    "ok": True,
                    "type": "release_samples",
                    **worker.release_samples(),
                }
            else:
                result = {"ok": False, "error": f"unknown command: {command!r}"}
        except Exception as exc:  # noqa: BLE001 - control protocol must report
            result = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        result["id"] = request_id
        emit(result)

    worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
