"""Coordination between operator actions and server-side self-test runs.

The self-test opens sample descriptors and (historically) called the global
``release_samples`` command in its cleanup, which could close descriptors that
an operator opened manually or that belonged to a new post-restart worker
generation.  This module is the single authority that makes the following
rules atomic:

* a self-test can only start when no other run is active and no sample
  descriptors are currently retained;
* while a run is live (not already interrupted), manual open/exhaust calls are
  rejected with :class:`ConflictError` (HTTP 409);
* release/restart always proceed, but first mark the active run aborted;
* every controller command issued by a run goes through :meth:`command`, which
  re-checks the abort flag and the worker generation under the same lock that
  release/restart/manual-open use, so a late step can never open or close
  samples after it has been interrupted;
* each run carries a unique ``owner`` tag; uninterrupted cleanup asks the
  worker to close only descriptors with that tag.
"""

from __future__ import annotations

import copy
import itertools
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .controller import EchoController


class ConflictError(RuntimeError):
    """An API operation conflicts with the current self-test/sample state."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": self.code,
            "code": self.code,
            "detail": self.message,
            "message": self.message,
        }
        body.update(self.extra)
        return body


class SelfTestAborted(RuntimeError):
    """Raised inside a run when an operator action interrupted it."""

    def __init__(self, reason: str, run: "SelfTestRun") -> None:
        super().__init__(reason)
        self.reason = reason
        self.run_id = run.run_id


_SEQ = itertools.count(1)


class SelfTestRun:
    """Identity and live report of one self-test execution."""

    def __init__(self, *, generation: int, run_id: str | None = None) -> None:
        n = next(_SEQ)
        self.run_id = run_id or f"selftest-{n}-{uuid.uuid4().hex[:8]}"
        # Tag stamped onto every sample descriptor this run opens.
        self.owner = f"selftest:{self.run_id}"
        self.generation = generation
        self.started_unix = time.time()
        self.abort_event = threading.Event()
        self.abort_reason: str | None = None
        self.abort_by: str | None = None
        self.state: str = "running"  # running | completed | failed | aborted
        self.finished: bool = False
        self.report_lock = threading.Lock()
        self.report: dict[str, Any] = {
            "ok": False,
            "state": "running",
            "status": "running",
            "aborted": False,
            "run_id": self.run_id,
            "owner": self.owner,
            "started_unix": self.started_unix,
            "steps": {},
            "checks": {},
        }

    # -- locked report mutation helpers (safe for concurrent GETs) ---------

    def put_step(self, key: str, value: Any) -> None:
        with self.report_lock:
            self.report["steps"][key] = value

    def put_check(self, key: str, value: bool) -> None:
        with self.report_lock:
            self.report["checks"][key] = bool(value)

    def update_report(self, **fields: Any) -> None:
        with self.report_lock:
            self.report.update(fields)

    def report_snapshot(self) -> dict[str, Any]:
        with self.report_lock:
            return copy.deepcopy(self.report)

    def identity(self) -> dict[str, Any]:
        abort_requested = self.abort_event.is_set()
        if self.state == "running" and abort_requested:
            effective_state = "aborting"
        else:
            effective_state = self.state
        return {
            "run_id": self.run_id,
            "owner": self.owner,
            "generation": self.generation,
            "state": effective_state,
            "status": effective_state,
            "abort_requested": abort_requested,
            "abort_reason": self.abort_reason,
            "abort_by": self.abort_by,
            "started_unix": self.started_unix,
            "finished": self.finished,
        }


class SelfTestManager:
    """Process-wide registry/lock for the at-most-one self-test run."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: SelfTestRun | None = None
        self._last: SelfTestRun | None = None

    # ------------------------------------------------------------------ begin

    def begin(self, controller: "EchoController") -> SelfTestRun:
        """Validate preconditions and register a new active run.

        Must be paired with :meth:`finish` by the caller.
        """
        with self._lock:
            current = self._active
            if current is not None and not current.finished:
                if current.abort_event.is_set():
                    raise ConflictError(
                        "self_test_stopping",
                        "a self-test is already being interrupted; "
                        "retry after its report is returned",
                        self_test=current.identity(),
                    )
                raise ConflictError(
                    "self_test_running",
                    "a self-test is already running",
                    self_test=current.identity(),
                )

            # A self-test must never adopt/close an operator's descriptors.
            status = controller.request("status")
            open_samples = int(status.get("open_sample_files") or 0)
            if open_samples > 0:
                raise ConflictError(
                    "manual_samples_open",
                    f"{open_samples} sample log descriptor(s) are already open; "
                    "release them (or restart the echo process) before self-test",
                    open_sample_files=open_samples,
                    sample_owners=status.get("sample_owners"),
                )

            run = SelfTestRun(generation=int(getattr(controller, "generation", 0)))
            self._active = run
            return run

    def finish(self, run: SelfTestRun, terminal_state: str) -> None:
        with self._lock:
            run.state = terminal_state
            run.finished = True
            run.update_report(
                state=terminal_state,
                status=terminal_state,
                finished_unix=time.time(),
            )
            if terminal_state == "aborted":
                run.update_report(aborted=True, ok=False)
            if self._active is run:
                self._active = None
            self._last = run

    # ------------------------------------------------------------- interruption

    def interrupt(self, reason: str, *, by: str | None = None) -> SelfTestRun | None:
        with self._lock:
            return self._interrupt_locked(reason, by=by)

    def _interrupt_locked(
        self, reason: str, *, by: str | None = None
    ) -> SelfTestRun | None:
        run = self._active
        if run is None or run.finished:
            return None
        if not run.abort_event.is_set():
            run.abort_reason = reason
            run.abort_by = by
            run.abort_event.set()
            run.update_report(
                abort_reason=reason,
                abort_by=by,
                state="aborting",
                status="aborting",
            )
        return run

    # ------------------------------------------------- operator entry points

    def manual_open_command(
        self,
        controller: "EchoController",
        command: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Open/exhaust entry point: 409 while a live run is in progress."""
        with self._lock:
            run = self._active
            if (
                run is not None
                and not run.finished
                and not run.abort_event.is_set()
            ):
                raise ConflictError(
                    "self_test_running",
                    "cannot open sample descriptors while a self-test is running; "
                    "release samples or restart the worker to interrupt it first",
                    self_test=run.identity(),
                )
            # Atomic with respect to begin()/interrupt()/restart(): the worker
            # command completes while we hold the coordination lock.
            return controller.request(command, payload)

    def manual_release(self, controller: "EchoController") -> dict[str, Any]:
        """Release all samples; this also interrupts a running self-test."""
        with self._lock:
            interrupted = self._interrupt_locked(
                "operator called release_samples", by="release"
            )
            result = controller.request("release_samples")
            result["self_test_interrupted"] = (
                interrupted.run_id if interrupted is not None else None
            )
            return result

    def manual_restart(self, controller: "EchoController") -> dict[str, Any]:
        """Restart the worker; this also interrupts a running self-test."""
        with self._lock:
            interrupted = self._interrupt_locked(
                "operator restarted the echo worker", by="restart"
            )
            ready = controller.restart()
            return {
                "ready": ready,
                "generation": int(getattr(controller, "generation", 0)),
                "self_test_interrupted": (
                    interrupted.run_id if interrupted is not None else None
                ),
            }

    # ------------------------------------------------- run-issued commands

    def _ensure_run_locked(
        self, run: SelfTestRun, controller: "EchoController"
    ) -> None:
        if run.abort_event.is_set():
            raise SelfTestAborted(run.abort_reason or "self-test interrupted", run)
        current = self._active
        if current is not None and current is not run:
            raise SelfTestAborted("self-test run was superseded", run)
        generation = int(getattr(controller, "generation", run.generation))
        if generation != run.generation:
            run.abort_event.set()
            run.abort_reason = run.abort_reason or (
                f"echo worker generation changed: "
                f"{run.generation} -> {generation}"
            )
            raise SelfTestAborted(run.abort_reason, run)

    def command(
        self,
        run: SelfTestRun,
        controller: "EchoController",
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Issue a worker command only if the run has not been interrupted.

        The check and the command run while holding the same lock that
        :meth:`manual_release`/:meth:`manual_restart` hold across their
        interrupt+operation, closing the check-then-act race.
        """
        with self._lock:
            self._ensure_run_locked(run, controller)
            return controller.request(command, payload, timeout=timeout)

    def cpu_sample(
        self,
        run: SelfTestRun,
        controller: "EchoController",
        seconds: float,
    ) -> dict[str, Any]:
        """Procfs CPU sample; the blocking sleep does not hold the lock.

        Release/restart therefore never waits for the CPU sampling window.
        Both before and after the sleep we verify the run is still current;
        an interrupt during the sleep is observed before any later step.
        """
        with self._lock:
            self._ensure_run_locked(run, controller)
        sample = controller.accept_loop_sample(seconds)
        with self._lock:
            self._ensure_run_locked(run, controller)
        return sample

    # ------------------------------------------------------------ snapshots

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
            if active is not None and not active.finished:
                info = active.identity()
                info["active"] = True
                info["report"] = active.report_snapshot()
                return info
            last = self._last
            return {
                "active": False,
                "state": "idle" if last is None else f"last:{last.state}",
                "last_self_test": self._last_summary_locked(),
            }

    def _last_summary_locked(self) -> dict[str, Any] | None:
        last = self._last
        if last is None:
            return None
        report = last.report_snapshot()
        return {
            **last.identity(),
            "ok": report.get("ok"),
            "aborted": report.get("aborted"),
            "error": report.get("error"),
            "abort_reason": report.get("abort_reason"),
            "checks": report.get("checks"),
        }
