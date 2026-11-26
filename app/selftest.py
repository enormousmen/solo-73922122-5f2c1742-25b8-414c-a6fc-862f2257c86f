"""Server-side end-to-end check for the EMFILE drill.

Concurrency contract (see :mod:`app.coordination`):

* a run is created by :class:`~app.coordination.SelfTestManager` only when no
  manual descriptors are open and no other run is live;
* every worker command and the CPU sample go through the manager's gates, so a
  manual release/restart that aborts the run prevents all late steps from
  opening or closing sample descriptors;
* all descriptors this run opens are tagged with ``run.owner`` and cleanup
  calls ``release_samples`` with that owner, so an uninterrupted run closes
  only its own descriptors even if other tags somehow coexist.
"""

from __future__ import annotations

import socket
import time
from typing import Any

from .controller import EchoController
from .coordination import SelfTestAborted, SelfTestManager, SelfTestRun
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
    manager: SelfTestManager | None = None,
    run: SelfTestRun | None = None,
) -> dict[str, Any]:
    """Exercise old-conn echo, EMFILE probe, counter, CPU, and recovery.

    Returns a report dict.  ``report["state"]`` is one of ``completed``,
    ``failed`` or ``aborted``; ``report["ok"]`` is true only when the run
    completed without interruption and every check passed.
    """
    if run is None:
        # Direct/programmatic callers (unit checks) do not get the HTTP
        # conflict registry, but still get an owner tag and owner-only cleanup.
        run = SelfTestRun(
            generation=int(getattr(controller, "generation", 0)),
            run_id=f"standalone-{time.time_ns()}",
        )

    report = run.report
    run.update_report(
        host=host,
        port=port,
        cpu_sample_seconds=cpu_sample_seconds,
    )

    def worker_request(
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        if manager is not None:
            return manager.command(
                run, controller, command, payload, timeout=timeout
            )
        return controller.request(command, payload, timeout=timeout)

    def cpu_sample(seconds: float) -> dict[str, Any]:
        if manager is not None:
            return manager.cpu_sample(run, controller, seconds)
        return controller.accept_loop_sample(seconds)

    old: socket.socket | None = None

    try:
        initial = worker_request("status")
        before_emfile = int(initial["metrics"]["emfile_total"])
        run.put_step("initial_emfile_total", before_emfile)

        old = socket.create_connection((host, port), timeout=2.0)
        _assert_echo(old, b"selftest-old-before-fault\n")
        run.put_check("old_echo_before_exhaust", True)

        # Tag every descriptor opened by the exhaust step with this run owner.
        exhaust = worker_request(
            "exhaust", {"owner": run.owner}, timeout=15.0
        )
        run.put_step(
            "exhaust",
            {
                key: exhaust.get(key)
                for key in (
                    "open_sample_files",
                    "open_fd_count",
                    "fd_table_full",
                    "fill_error",
                    "sample_owners",
                )
            },
        )
        if not exhaust.get("fd_table_full"):
            raise AssertionError("descriptor table was not reported full")

        _assert_echo(old, b"selftest-old-during-emfile\n")
        run.put_check("old_echo_during_fault", True)

        probe = run_tcp_probe(
            host,
            port,
            send_payload=False,
            timeout_seconds=1.0,
        )
        run.put_step("new_probe_during_fault", probe)
        probe_ok = (
            probe.get("outcome") in ("eof", "reset")
            and bool(probe.get("within_one_second"))
        )
        run.put_check("probe_eof_or_reset_within_one_second", probe_ok)
        if not probe_ok:
            raise AssertionError(f"bad EMFILE probe result: {probe}")

        status = worker_request("status")
        emfile_total = int(status["metrics"]["emfile_total"])
        run.put_step("emfile_total", emfile_total)
        emfile_ok = emfile_total > before_emfile
        run.put_check("real_emfile_count_increased", emfile_ok)
        if not emfile_ok:
            raise AssertionError("EMFILE counter did not increase")

        cpu = cpu_sample(cpu_sample_seconds)
        run.put_step("accept_loop_cpu", cpu)
        cpu_ok = not cpu["busy_spin_suspected"]
        run.put_check("accept_loop_not_busy", cpu_ok)
        if cpu["busy_spin_suspected"]:
            raise AssertionError("accept loop appears to busy-wait")

        # Happy-path cleanup: close only this run's own descriptors.
        release = worker_request(
            "release_samples", {"owner": run.owner}
        )
        run.put_step(
            "release",
            {
                key: release.get(key)
                for key in (
                    "closed_sample_files",
                    "owner_filter",
                    "open_sample_files",
                    "listener_fd",
                    "open_fd_count",
                )
            },
        )

        recovery_payload = f"selftest-recovery-{time.time()}".encode()
        recovery = run_tcp_probe(
            host,
            port,
            payload=recovery_payload.decode(),
            send_payload=True,
            expect_echo=True,
            timeout_seconds=2.0,
        )
        run.put_step("recovery_probe", recovery)
        recovery_ok = bool(recovery.get("echo_matches"))
        run.put_check("new_connection_echoes_after_release", recovery_ok)
        if not recovery_ok:
            raise AssertionError(f"recovery echo failed: {recovery}")

        all_ok = all(report["checks"].values())
        run.update_report(ok=all_ok, state="completed", status="completed")
        run.state = "completed"

    except SelfTestAborted as exc:
        # The operator's release/restart already closed/replaced the sample
        # state.  Do NOT issue any further open/close command here.
        run.update_report(
            ok=False,
            aborted=True,
            state="aborted",
            status="aborted",
            abort_reason=exc.reason,
        )
        run.state = "aborted"

    except Exception as exc:  # noqa: BLE001 - report failure to API caller
        run.update_report(
            error_type=type(exc).__name__,
            error=str(exc),
            state="failed",
            status="failed",
        )

        # Uninterrupted failure cleanup: owner-scoped only.  If an interrupt
        # lands while we are handling the failure, the gated call raises
        # SelfTestAborted and we must not close anything.
        try:
            cleanup = worker_request(
                "release_samples", {"owner": run.owner}
            )
            run.put_step(
                "cleanup_release",
                {
                    "closed_sample_files": cleanup.get("closed_sample_files"),
                    "owner_filter": cleanup.get("owner_filter"),
                },
            )
        except SelfTestAborted as abort_exc:
            run.update_report(
                ok=False,
                aborted=True,
                state="aborted",
                status="aborted",
                abort_reason=abort_exc.reason,
            )
            run.state = "aborted"
        except Exception as cleanup_exc:  # noqa: BLE001
            run.update_report(cleanup_error=repr(cleanup_exc))

        if run.state != "aborted":
            run.state = "failed"
        run.update_report(ok=False)

    finally:
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        run.update_report(finished_unix=time.time())

    # Snapshot after the finally so finished_unix is present.
    return run.report_snapshot()
