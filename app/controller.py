"""Controller for the out-of-process Linux TCP echo drill worker."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("fd_drill.controller")


class EchoControllerError(RuntimeError):
    """Raised when the echo worker cannot execute a control request."""


class EchoController:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sample_dir: str | Path,
        nofile_limit: int,
        worker_path: str | Path | None = None,
        startup_timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.sample_dir = Path(sample_dir)
        self.nofile_limit = int(nofile_limit)
        self.worker_path = Path(
            worker_path
            or Path(__file__).resolve().parent / "echo_worker.py"
        )
        self.startup_timeout = startup_timeout

        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._request_id = 0
        self.ready: dict[str, Any] | None = None
        self.start_error: str | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.is_alive() and self.ready is not None:
                return self.ready

            self.sample_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable,
                "-I",
                str(self.worker_path),
                "--host",
                self.host,
                "--port",
                str(self.port),
                "--nofile-limit",
                str(self.nofile_limit),
                "--sample-dir",
                str(self.sample_dir),
            ]
            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            LOG.info("starting echo worker: %s", " ".join(cmd))
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                bufsize=1,
                env=env,
            )
            self._lines = queue.Queue()
            self._reader = threading.Thread(
                target=self._read_stdout,
                name="echo-worker-stdout",
                daemon=True,
            )
            self._reader.start()

            try:
                line = self._lines.get(timeout=self.startup_timeout)
                if line is None:
                    raise EchoControllerError("echo worker exited before ready")
                ready = json.loads(line)
                if ready.get("type") != "ready" or not ready.get("ok"):
                    raise EchoControllerError(f"unexpected ready message: {ready}")
                self.ready = ready
                self.start_error = None
                return ready
            except Exception as exc:
                self.start_error = f"{type(exc).__name__}: {exc}"
                try:
                    self.stop()
                except Exception:
                    LOG.exception("failed to stop worker after startup error")
                raise

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._lines.put(None)
            return
        try:
            for line in proc.stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def request(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        with self._lock:
            if not self.is_alive():
                raise EchoControllerError(
                    f"echo worker is not running: {self.start_error or 'exited'}"
                )
            assert self._proc is not None and self._proc.stdin is not None

            self._request_id += 1
            request_id = f"req-{self._request_id}"
            message = {"id": request_id, "cmd": command}
            if payload:
                message.update(payload)

            try:
                self._proc.stdin.write(json.dumps(message) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise EchoControllerError(f"cannot write to echo worker: {exc}") from exc

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EchoControllerError(
                        f"timeout waiting for echo worker response to {command}"
                    )
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty as exc:
                    raise EchoControllerError(
                        f"timeout waiting for echo worker response to {command}"
                    ) from exc
                if line is None:
                    code = self._proc.poll() if self._proc is not None else None
                    raise EchoControllerError(
                        f"echo worker exited (code={code}) before responding to {command}"
                    )
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("non-JSON worker line: %r", line.rstrip())
                    continue
                if response.get("id") != request_id:
                    LOG.debug("discarding mismatched worker message: %s", response)
                    continue
                if not response.get("ok", False):
                    raise EchoControllerError(
                        response.get("error") or f"worker command {command} failed"
                    )
                return response

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2.0)
                    except Exception:
                        LOG.exception("unable to kill echo worker pid=%s", proc.pid)
            finally:
                self._proc = None
                self.ready = None

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start()

    def accept_loop_sample(self, seconds: float = 1.0) -> dict[str, Any]:
        """Sample CPU/state of the worker's accept thread from procfs."""
        if not self.is_alive() or not self.ready:
            raise EchoControllerError("echo worker is not running")
        pid = int(self.ready["pid"])
        tid = int(self.ready["accept_thread_tid"])
        before = _read_thread_stat(pid, tid)
        time.sleep(max(0.0, float(seconds)))
        after = _read_thread_stat(pid, tid)
        hz = os.sysconf("SC_CLK_TCK")
        cpu_seconds = (
            (after["utime_ticks"] + after["stime_ticks"])
            - (before["utime_ticks"] + before["stime_ticks"])
        ) / hz
        wchan = None
        try:
            wchan = Path(f"/proc/{pid}/task/{tid}/wchan").read_text().strip()
        except OSError:
            wchan = None
        return {
            "sample_seconds": seconds,
            "cpu_seconds": round(cpu_seconds, 6),
            "cpu_fraction": round(cpu_seconds / seconds, 6) if seconds else None,
            "busy_spin_suspected": cpu_seconds > max(0.02, seconds * 0.2),
            "state_before": before["state"],
            "state_after": after["state"],
            "wchan": wchan,
            "ticks_per_second": hz,
            "note": (
                "Blocked in select()/kernel wait is expected while the fault is "
                "idle. A single pending probe still causes one EMFILE and reap."
            ),
        }


def _read_thread_stat(pid: int, tid: int) -> dict[str, Any]:
    raw = Path(f"/proc/{pid}/task/{tid}/stat").read_text()
    rest = raw[raw.rfind(")") + 2 :].split()
    # Field 3 is state, field 14/15 are utime/stime; rest starts at field 3.
    return {
        "state": rest[0],
        "utime_ticks": int(rest[11]),
        "stime_ticks": int(rest[12]),
    }
