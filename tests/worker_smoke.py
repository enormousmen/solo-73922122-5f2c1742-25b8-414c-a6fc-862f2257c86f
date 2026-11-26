#!/usr/bin/env python3
"""Stdlib-only integration check for echo_worker.py.

This does not exercise FastAPI; it validates the Linux FD/EMFILE behavior and is
usable in minimal environments where FastAPI is not installed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "app" / "echo_worker.py"


def send(proc: subprocess.Popen[bytes], payload: dict) -> dict:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write((json.dumps(payload) + "\n").encode())
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("worker closed stdout")
    return json.loads(line)


def wait_echo(sock: socket.socket, payload: bytes, timeout: float = 2.0) -> bytes:
    sock.settimeout(timeout)
    sock.sendall(payload)
    chunks = []
    remaining = len(payload)
    while remaining:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("unexpected EOF while expecting echo")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def probe_eof(port: int, timeout: float = 1.0) -> tuple[str, float]:
    started = time.perf_counter()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        connected = time.perf_counter()
        s.settimeout(timeout)
        try:
            data = s.recv(1)
            elapsed = time.perf_counter() - connected
            if data == b"":
                return "eof", elapsed
            return f"data:{data!r}", elapsed
        except ConnectionResetError:
            return "reset", time.perf_counter() - connected
        except socket.timeout:
            return "timeout", time.perf_counter() - started


def thread_cpu(pid: int, tid: int) -> float:
    raw = Path(f"/proc/{pid}/task/{tid}/stat").read_text()
    rest = raw[raw.rfind(")") + 2 :].split()
    utime = int(rest[11])
    stime = int(rest[12])
    hz = os.sysconf("SC_CLK_TCK")
    return (utime + stime) / hz


def main() -> int:
    sample_dir = Path("/tmp/fd-emfile-worker-selftest")
    sample_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(WORKER),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--nofile-limit",
            "32",
            "--sample-dir",
            str(sample_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    try:
        assert proc.stdout is not None
        ready = json.loads(proc.stdout.readline())
        print("ready:", json.dumps(ready, indent=2))
        port = ready["port"]
        tid = ready["accept_thread_tid"]

        old = socket.create_connection(("127.0.0.1", port), timeout=2)
        assert wait_echo(old, b"before-exhaust\n") == b"before-exhaust\n"
        print("old connection echo before exhaustion: OK")

        result = send(proc, {"id": "exhaust", "cmd": "exhaust"})
        print("exhaust:", json.dumps({k: result[k] for k in (
            "ok", "opened_this_call", "open_sample_files", "open_fd_count",
            "fd_count_note", "fd_table_full", "fill_error"
        )}, indent=2))
        assert result["ok"], result
        assert result["fd_table_full"], result

        assert wait_echo(old, b"old-conn-still-live\n") == b"old-conn-still-live\n"
        print("old connection echo during exhaustion: OK")

        outcome, elapsed = probe_eof(port, timeout=1.0)
        print(f"new probe: {outcome!r} after {elapsed*1000:.1f} ms")
        assert outcome in ("eof", "reset"), (outcome, elapsed)
        assert elapsed <= 1.0, elapsed

        status = send(proc, {"id": "status", "cmd": "status"})
        print("metrics:", json.dumps(status["metrics"], indent=2))
        print("rescue_fd:", status["rescue_fd"], "fd_count:", status["open_fd_count"])
        emfile = status["metrics"]["emfile_total"]
        print("emfile_total:", emfile)
        assert emfile >= 1, status

        cpu0 = thread_cpu(proc.pid, tid)
        time.sleep(1.0)
        cpu1 = thread_cpu(proc.pid, tid)
        burn = cpu1 - cpu0
        print(f"accept thread CPU over idle fault second: {burn*1000:.1f} ms")
        assert burn < 0.05, burn

        released = send(proc, {"id": "release", "cmd": "release_samples"})
        print("release:", released["closed_sample_files"], "files")
        assert released["ok"]

        new = socket.create_connection(("127.0.0.1", port), timeout=2)
        payload = f"after-release-{time.time()}".encode()
        assert wait_echo(new, payload) == payload
        new.close()
        print("new connection echo after release: OK")

        old.close()
        print("WORKER SELFTEST PASSED")
        return 0
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
