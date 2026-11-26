#!/usr/bin/env python3
"""API demo: operator actions interrupting / conflicting with the self-test.

Stdlib only.  Run after ``docker compose up --build`` (or a local uvicorn):

    python3 scripts/demo_interrupt.py
    python3 scripts/demo_interrupt.py --api-base http://127.0.0.1:8000

It verifies the delivery contract end to end:

1. manual sample descriptors present  -> POST /self-test == 409 and the
   rejected start does not close them;
2. while a self-test is running: a second self-test, sample open and exhaust
   all return 409, while GET /status stays 200;
3. POST /release interrupts the run; opening new samples immediately after
   succeeds and those new descriptors survive the stale self-test;
4. POST /restart likewise interrupts the run and late steps never touch the
   new worker generation / its newly opened samples;
5. an uninterrupted self-test still passes and cleans up after itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"_raw": raw}
            return exc.code, parsed


def extract_report(payload: Any) -> dict[str, Any]:
    """Find the self-test report across all supported response envelopes."""
    if not isinstance(payload, dict):
        return {}
    if payload.get("aborted") is True or payload.get("state") in (
        "aborted",
        "completed",
        "failed",
    ):
        return payload
    for key in ("detail", "report"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = extract_report(nested)
            if found:
                return found
    return payload


def selftest_result_is_aborted(status: int, payload: Any) -> bool:
    report = extract_report(payload)
    return bool(
        report.get("aborted") is True
        or report.get("state") == "aborted"
        or report.get("status") == "aborted"
    )


def start_selftest(api: Api) -> tuple[threading.Thread, dict[str, Any]]:
    holder: dict[str, Any] = {}

    def worker() -> None:
        status, payload = api.call("POST", "/api/v1/drill/self-test", timeout=30)
        holder["status"] = status
        holder["payload"] = payload

    thread = threading.Thread(target=worker, name="selftest-call", daemon=True)
    thread.start()
    return thread, holder


def wait_active(api: Api, timeout: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        code, snap = api.call("GET", "/api/v1/drill/self-test")
        last = snap if isinstance(snap, dict) else {}
        if code == 200 and last.get("active"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"self-test did not become active: {last}")


def open_sample_count(api: Api) -> int:
    code, status = api.call("GET", "/api/v1/status")
    assert code == 200, status
    return int(status.get("open_sample_files") or 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    api = Api(args.api_base)

    failures: list[str] = []

    def check(name: str, condition: bool, detail: Any = "") -> None:
        mark = "PASS" if condition else "FAIL"
        print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    # Health + clean slate --------------------------------------------------
    code, health = api.call("GET", "/healthz")
    if code != 200 or not health.get("ok"):
        print(f"API/echo unhealthy: {code} {health}", file=sys.stderr)
        return 2
    print(f"health OK (echo pid={health.get('echo_pid')})")
    api.call("POST", "/api/v1/drill/release")

    # 1. manual samples block self-test with 409 -----------------------------
    print("\n== Scenario 1: pre-existing manual samples -> 409 ==")
    code, opened = api.call(
        "POST", "/api/v1/sample-files/open", {"count": 2}
    )
    check("manual open count=2 -> 200", code == 200, (code, opened))
    check("two descriptors retained", open_sample_count(api) == 2)

    code, conflict = api.call("POST", "/api/v1/drill/self-test")
    check("self-test with manual samples -> 409", code == 409, (code, conflict))
    check(
        "conflict code is manual_samples_open",
        isinstance(conflict, dict) and conflict.get("code") == "manual_samples_open",
        conflict,
    )
    check(
        "rejected self-test did not close manual descriptors",
        open_sample_count(api) == 2,
        f"count={open_sample_count(api)}",
    )
    code, released = api.call("POST", "/api/v1/drill/release")
    check("release -> 200", code == 200, (code, released))
    check("descriptors released", open_sample_count(api) == 0)

    # 2/3. running self-test: conflict matrix + release interrupt ------------
    print("\n== Scenario 2: running self-test conflicts, then release interrupt ==")
    thread, holder = start_selftest(api)
    try:
        snap = wait_active(api)
        run_id = snap.get("run_id")
        print(f"  self-test active: {run_id}")

        code, body = api.call("POST", "/api/v1/drill/self-test")
        check("second self-test -> 409", code == 409, code)

        code, body = api.call(
            "POST", "/api/v1/sample-files/open", {"count": 1}
        )
        check("manual sample open during run -> 409", code == 409, code)

        code, body = api.call("POST", "/api/v1/drill/exhaust")
        check("exhaust during run -> 409", code == 409, code)

        code, status = api.call("GET", "/api/v1/status")
        check("GET /status during run -> 200", code == 200, code)
        check(
            "status reports active self-test",
            isinstance(status, dict)
            and isinstance(status.get("self_test"), dict)
            and status["self_test"].get("active") is True,
        )

        code, release = api.call("POST", "/api/v1/drill/release")
        check("release during run -> 200", code == 200, (code, release))
        check(
            "release reports the interrupted run id",
            isinstance(release, dict)
            and release.get("self_test_interrupted") == run_id,
            release,
        )

        # The moment release completes, manual opens must work.
        code, opened = api.call(
            "POST", "/api/v1/sample-files/open", {"count": 3}
        )
        check("open count=3 immediately after release -> 200", code == 200, (code, opened))
    finally:
        thread.join(timeout=10.0)
        check("self-test call returned after interrupt", not thread.is_alive())

    status_code = holder.get("status")
    payload = holder.get("payload")
    report = extract_report(payload)
    print(
        f"  self-test completion: HTTP {status_code}, "
        f"state={report.get('state')}, aborted={report.get('aborted')}, "
        f"reason={report.get('abort_reason')!r}"
    )
    check(
        "interrupted self-test reports abort",
        selftest_result_is_aborted(status_code or 0, payload),
        holder,
    )

    # Give a misbehaving stale run time to execute a late cleanup.
    time.sleep(0.8)
    surviving = open_sample_count(api)
    check(
        "three new manual descriptors survive the stale self-test",
        surviving == 3,
        f"open_sample_files={surviving}",
    )
    code, status = api.call("GET", "/api/v1/status")
    owners = (status or {}).get("sample_owners") if isinstance(status, dict) else None
    check("survivors are owner=manual", owners == {"manual": 3}, owners)

    # Echo still works with only three extra descriptors.
    code, probe = api.call(
        "POST",
        "/api/v1/probe",
        {"payload": "after-release-interrupt", "send_payload": True, "expect_echo": True},
    )
    check(
        "echo works after release interrupt + manual samples",
        code == 200 and bool((probe or {}).get("echo_matches")),
        (code, probe),
    )
    api.call("POST", "/api/v1/drill/release")
    check("cleanup release -> 0 samples", open_sample_count(api) == 0)

    # 4. restart interrupt ---------------------------------------------------
    print("\n== Scenario 3: restart interrupts self-test; new generation untouched ==")
    before_pid = api.call("GET", "/healthz")[1].get("echo_pid")
    thread, holder = start_selftest(api)
    try:
        snap = wait_active(api)
        run_id = snap.get("run_id")
        print(f"  self-test active: {run_id} (pid {before_pid})")
        code, restart = api.call("POST", "/api/v1/drill/restart")
        check("restart during run -> 200", code == 200, (code, restart))
        check(
            "restart reports the interrupted run id",
            isinstance(restart, dict)
            and restart.get("self_test_interrupted") == run_id,
            restart,
        )
        new_pid = (
            (restart.get("ready") or {}).get("pid")
            if isinstance(restart, dict)
            else None
        )
        check("restart produced a new echo pid", bool(new_pid) and new_pid != before_pid, restart)

        code, opened = api.call(
            "POST", "/api/v1/sample-files/open", {"count": 2}
        )
        check("open count=2 immediately after restart -> 200", code == 200, (code, opened))
    finally:
        thread.join(timeout=12.0)
        check("self-test call returned after restart", not thread.is_alive())

    status_code = holder.get("status")
    payload = holder.get("payload")
    report = extract_report(payload)
    print(
        f"  self-test completion: HTTP {status_code}, "
        f"state={report.get('state')}, aborted={report.get('aborted')}, "
        f"reason={report.get('abort_reason')!r}"
    )
    check(
        "restarted self-test reports abort",
        selftest_result_is_aborted(status_code or 0, payload),
        holder,
    )
    time.sleep(0.8)
    surviving = open_sample_count(api)
    check(
        "two descriptors on the new worker survive the stale self-test",
        surviving == 2,
        f"open_sample_files={surviving}",
    )
    code, health = api.call("GET", "/healthz")
    check("echo worker is the restarted one", health.get("echo_pid") == new_pid, health)
    code, probe = api.call(
        "POST",
        "/api/v1/probe",
        {"payload": "after-restart-interrupt", "send_payload": True, "expect_echo": True},
    )
    check(
        "echo works on restarted worker",
        code == 200 and bool((probe or {}).get("echo_matches")),
        (code, probe),
    )
    api.call("POST", "/api/v1/drill/release")
    check("cleanup release -> 0 samples", open_sample_count(api) == 0)

    # 5. happy path regression ----------------------------------------------
    print("\n== Scenario 4: uninterrupted self-test passes and self-cleans ==")
    started = time.monotonic()
    code, result = api.call("POST", "/api/v1/drill/self-test", timeout=30)
    elapsed = time.monotonic() - started
    report = extract_report(result)
    check("happy self-test -> 200", code == 200, (code, result))
    check("report ok=true", report.get("ok") is True, report.get("error"))
    checks = report.get("checks") or {}
    for name, value in checks.items():
        check(f"check {name}", value is True)
    check("all six expected checks present", len(checks) >= 6, list(checks))
    check("self-test cleaned its own descriptors", open_sample_count(api) == 0)
    print(f"  self-test elapsed {elapsed:.2f}s")

    print("\n" + ("ALL INTERRUPTION SCENARIOS PASSED" if not failures else f"FAILURES: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
