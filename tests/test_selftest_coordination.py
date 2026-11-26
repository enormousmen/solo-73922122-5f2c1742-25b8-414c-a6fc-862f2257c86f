"""Hermetic regression tests for self-test ownership and interruption.

These do not need FastAPI, a live port, or a real echo worker: a recording
fake controller stands in for the worker.  They pin down the invariants that
are awkward to race over HTTP:

* an uninterrupted *failed* self-test issues an owner-scoped release and never
  a global release;
* once a run is interrupted, gated commands are refused before they reach the
  controller, so late steps can neither open nor close descriptors;
* the manager enforces the 409 conflict rules and worker generation fencing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.coordination import (  # noqa: E402
    ConflictError,
    SelfTestAborted,
    SelfTestManager,
)
from app.selftest import run_self_test  # noqa: E402


class FakeController:
    def __init__(self, *, open_sample_files: int = 0, generation: int = 1) -> None:
        self.generation = generation
        self._open_sample_files = open_sample_files
        self.calls: list[tuple[str, dict | None]] = []
        # Optional callback invoked inside request(), e.g. to simulate an
        # operator interrupt arriving during a worker command.
        self.on_request = None

    def request(self, command, payload=None, *, timeout=10.0):
        self.calls.append((command, payload))
        if self.on_request is not None:
            self.on_request(command, payload)
        if command == "status":
            return {
                "metrics": {"emfile_total": 0},
                "open_sample_files": self._open_sample_files,
                "sample_owners": (
                    {"manual": self._open_sample_files}
                    if self._open_sample_files
                    else {}
                ),
            }
        if command == "exhaust":
            return {"fd_table_full": True}
        if command == "release_samples":
            return {"closed_sample_files": 0, "owner_filter": (payload or {}).get("owner")}
        return {}

    def accept_loop_sample(self, seconds):
        return {"busy_spin_suspected": False}


def _commands(fake: FakeController) -> list[str]:
    return [name for name, _ in fake.calls]


def test_begin_conflicts_when_manual_samples_open():
    manager = SelfTestManager()
    fake = FakeController(open_sample_files=2)
    with pytest.raises(ConflictError) as exc_info:
        manager.begin(fake)
    assert exc_info.value.code == "manual_samples_open"
    # The precondition check is a status read only; nothing was mutated.
    assert _commands(fake) == ["status"]


def test_live_run_blocks_manual_open_and_second_begin():
    manager = SelfTestManager()
    fake = FakeController()
    run = manager.begin(fake)

    with pytest.raises(ConflictError) as exc_info:
        manager.manual_open_command(fake, "exhaust", {"owner": "manual"})
    assert exc_info.value.code == "self_test_running"

    with pytest.raises(ConflictError) as exc_info:
        manager.begin(fake)
    assert exc_info.value.code == "self_test_running"

    manager.finish(run, "completed")
    # After finish, manual opens are admitted again.
    manager.manual_open_command(fake, "open_samples", {"count": 1, "owner": "manual"})
    assert _commands(fake)[-1] == "open_samples"


def test_interrupt_allows_manual_open_but_blocks_late_run_commands():
    manager = SelfTestManager()
    fake = FakeController()
    run = manager.begin(fake)

    interrupted = manager.interrupt("unit-test interrupt", by="release")
    assert interrupted is run

    # Operator opens are allowed immediately after the interrupt request.
    manager.manual_open_command(fake, "open_samples", {"count": 3, "owner": "manual"})

    # But every run-issued command is rejected before reaching the worker.
    calls_before = list(fake.calls)
    with pytest.raises(SelfTestAborted):
        manager.command(run, fake, "exhaust", {"owner": run.owner})
    with pytest.raises(SelfTestAborted):
        manager.command(run, fake, "release_samples", {"owner": run.owner})
    assert fake.calls == calls_before  # no late open/close reached controller


def test_generation_change_fences_stale_run():
    manager = SelfTestManager()
    fake = FakeController(generation=1)
    run = manager.begin(fake)
    # Simulate restart having bumped the controller generation.
    fake.generation = 2
    with pytest.raises(SelfTestAborted):
        manager.command(run, fake, "status")
    assert run.abort_event.is_set()


def test_uninterrupted_failure_cleanup_is_owner_scoped_only():
    # Point at a closed local port: the run fails immediately after the
    # initial status read, so the except-branch cleanup is exercised.
    manager = SelfTestManager()
    fake = FakeController()
    run = manager.begin(fake)

    report = run_self_test(
        fake,
        host="127.0.0.1",
        port=1,  # privileged + almost certainly closed -> fast refusal
        cpu_sample_seconds=0.01,
        manager=manager,
        run=run,
    )
    manager.finish(run, report["state"])

    assert report["state"] == "failed"
    assert report["ok"] is False

    release_calls = [
        payload for name, payload in fake.calls if name == "release_samples"
    ]
    assert len(release_calls) == 1
    # Crucially: owner-scoped to this run, never a global close.
    assert release_calls[0] == {"owner": run.owner}
    for payload in release_calls:
        assert payload.get("owner") is not None


def test_worker_owner_scoped_release_keeps_manual_descriptors(tmp_path):
    from app.echo_worker import EchoWorker

    worker = EchoWorker(host="127.0.0.1", port=0, sample_dir=tmp_path)
    manual = worker.open_samples(count=2, owner="manual")
    selftest = worker.open_samples(count=3, owner="selftest:run-x")
    assert manual["open_sample_files"] == 2
    assert selftest["open_sample_files"] == 5

    result = worker.release_samples(owner="selftest:run-x")
    assert result["closed_sample_files"] == 3
    assert result["open_sample_files"] == 2

    # The surviving handles really are the manual ones and remain open.
    assert len(worker.sample_handles) == 2
    assert {r["owner"] for r in worker.sample_records} == {"manual"}
    for handle in worker.sample_handles:
        assert not handle.closed
        handle.write("still-writable\n")
        handle.flush()

    # A global release still closes everything (operator release semantics).
    all_closed = worker.release_samples()
    assert all_closed["closed_sample_files"] == 2
    assert worker.sample_handles == []


def test_abort_during_first_status_prevents_all_mutation():
    manager = SelfTestManager()
    fake = FakeController()
    run = manager.begin(fake)

    def interrupt_during_status(command, payload):
        if command == "status":
            manager.interrupt("arrived during status", by="release")

    fake.on_request = interrupt_during_status

    report = run_self_test(
        fake,
        host="127.0.0.1",
        port=1,
        cpu_sample_seconds=0.01,
        manager=manager,
        run=run,
    )
    manager.finish(run, report["state"])

    assert report["state"] == "aborted"
    assert report["aborted"] is True
    # Two reads only: begin()'s precondition status and the run's initial
    # status (where the interrupt landed).  No exhaust/open/release mutation
    # may have reached the worker, including the gated cleanup release.
    assert _commands(fake) == ["status", "status"]
    assert not any(name in ("exhaust", "open_samples", "release_samples") for name in _commands(fake))
