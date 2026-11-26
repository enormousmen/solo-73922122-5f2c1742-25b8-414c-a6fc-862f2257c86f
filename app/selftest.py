"""Server-side end-to-end check for the EMFILE drill."""

from __future__ import annotations

import socket
import time
from typing import Any

from .controller import EchoController
from .probes import run_tcp_probe


def _assert_echo(sock: socket.socket, payload: bytes) -> None:
    sock.settimeout(2.0)
    sock.sendall(payload)
    received = bytearray()
    while len(received) < len(payload):
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("unexpected EOF while waiting for echo")
        received.extend(chunk)
    if bytes(received) != payload:
        raise AssertionError("old connection echo mismatch")


def run_self_test(
    controller: EchoController,
    *,
    host: str,
    port: int,
    cpu_sample_seconds: float = 0.5,
) -> dict[str, Any]:
    """Exercise old-conn echo, EMFILE probe, counter, CPU, and recovery."""
    report: dict[str, Any] = {
        "ok": False,
        "host": host,
        "port": port,
        "started_unix": time.time(),
        "steps": {},
        "checks": {},
    }
    old: socket.socket | None = None
    released = False

    try:
        initial = controller.request("status")
        before_emfile = int(initial["metrics"]["emfile_total"])
        report["steps"]["initial_emfile_total"] = before_emfile

        old = socket.create_connection((host, port), timeout=2.0)
        _assert_echo(old, b"selftest-old-before-fault\n")
        report["checks"]["old_echo_before_exhaust"] = True

        exhaust = controller.request("exhaust", timeout=15.0)
        report["steps"]["exhaust"] = {
            key: exhaust.get(key)
            for key in (
                "open_sample_files",
                "open_fd_count",
                "fd_table_full",
                "fill_error",
            )
        }
        if not exhaust.get("fd_table_full"):
            raise AssertionError("descriptor table was not reported full")

        _assert_echo(old, b"selftest-old-during-emfile\n")
        report["checks"]["old_echo_during_fault"] = True

        probe = run_tcp_probe(
            host,
            port,
            send_payload=False,
            timeout_seconds=1.0,
        )
        report["steps"]["new_probe_during_fault"] = probe
        report["checks"]["probe_eof_or_reset_within_one_second"] = (
            probe.get("outcome") in ("eof", "reset")
            and bool(probe.get("within_one_second"))
        )
        if not report["checks"]["probe_eof_or_reset_within_one_second"]:
            raise AssertionError(f"bad EMFILE probe result: {probe}")

        status = controller.request("status")
        emfile_total = int(status["metrics"]["emfile_total"])
        report["steps"]["emfile_total"] = emfile_total
        report["checks"]["real_emfile_count_increased"] = emfile_total > before_emfile
        if not report["checks"]["real_emfile_count_increased"]:
            raise AssertionError("EMFILE counter did not increase")

        cpu = controller.accept_loop_sample(cpu_sample_seconds)
        report["steps"]["accept_loop_cpu"] = cpu
        report["checks"]["accept_loop_not_busy"] = not cpu["busy_spin_suspected"]
        if cpu["busy_spin_suspected"]:
            raise AssertionError("accept loop appears to busy-wait")

        release = controller.request("release_samples")
        released = True
        report["steps"]["release"] = {
            key: release.get(key)
            for key in ("closed_sample_files", "listener_fd", "open_fd_count")
        }

        recovery_payload = f"selftest-recovery-{time.time()}".encode()
        recovery = run_tcp_probe(
            host,
            port,
            payload=recovery_payload.decode(),
            send_payload=True,
            expect_echo=True,
            timeout_seconds=2.0,
        )
        report["steps"]["recovery_probe"] = recovery
        report["checks"]["new_connection_echoes_after_release"] = bool(
            recovery.get("echo_matches")
        )
        if not recovery.get("echo_matches"):
            raise AssertionError(f"recovery echo failed: {recovery}")

        report["ok"] = all(report["checks"].values())
        return report

    except Exception as exc:  # noqa: BLE001 - report failure to API caller
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        return report
    finally:
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        if not released:
            try:
                controller.request("release_samples")
            except Exception as exc:  # noqa: BLE001
                report["cleanup_error"] = repr(exc)
        report["finished_unix"] = time.time()
