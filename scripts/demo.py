#!/usr/bin/env python3
"""End-to-end demo for the FD/EMFILE drill.

Stdlib only.  Run after `docker compose up --build`:

    python3 scripts/demo.py

The script keeps one old TCP connection open throughout the fault, verifies
its echo, opens exactly one new probe and expects EOF/RST within one second,
reads the real EMFILE counter from FastAPI, checks accept-thread CPU, then
releases sample descriptors and verifies a brand-new connection echoes.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def api_call(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc


def assert_echo(sock: socket.socket, payload: bytes) -> None:
    sock.settimeout(2.0)
    sock.sendall(payload)
    received = bytearray()
    while len(received) < len(payload):
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("unexpected EOF while waiting for echo")
        received.extend(chunk)
    if bytes(received) != payload:
        raise AssertionError(f"echo mismatch: {received!r} != {payload!r}")


def probe_eof(host: str, port: int, timeout: float = 1.0) -> tuple[str, float]:
    started = time.perf_counter()
    sock = socket.create_connection((host, port), timeout=timeout)
    with sock:
        connected = time.perf_counter()
        sock.settimeout(timeout)
        try:
            first = sock.recv(1)
        except ConnectionResetError:
            return "RST", time.perf_counter() - connected
        except socket.timeout as exc:
            elapsed = time.perf_counter() - connected
            raise AssertionError(f"probe did not receive EOF/RST within {elapsed:.3f}s") from exc
        elapsed = time.perf_counter() - connected
        if first == b"":
            return "EOF", elapsed
        raise AssertionError(f"probe received unexpected byte {first!r} after {elapsed:.3f}s")


def successful_echo_connection(host: str, port: int, payload: bytes, deadline_s: float = 3.0) -> None:
    deadline = time.monotonic() + deadline_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5) as sock:
                assert_echo(sock, payload)
                return
        except (OSError, ConnectionError, AssertionError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"new connection did not recover and echo: {last_error!r}")


def wait_emfile_increment(base: str, before: int, timeout_s: float = 1.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = api_call(base, "GET", "/api/v1/metrics/emfile")
        if int(last["emfile_total"]) > before:
            return last
        time.sleep(0.02)
    raise AssertionError(f"EMFILE counter did not increase from {before}: {last}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--echo-host", default="127.0.0.1")
    parser.add_argument("--echo-port", type=int, default=9000)
    parser.add_argument("--keep-fault", action="store_true", help="Do not release files in finally")
    parser.add_argument("--skip-cpu-check", action="store_true")
    args = parser.parse_args()

    released_by_finally = False
    old: socket.socket | None = None
    try:
        health = api_call(args.api_base, "GET", "/healthz")
        print(f"[1/8] API health: {health}")
        if not health.get("ok"):
            raise AssertionError("API/echo process is unhealthy")

        before_metric = api_call(args.api_base, "GET", "/api/v1/metrics/emfile")
        before_emfile = int(before_metric["emfile_total"])
        print(f"[2/8] Initial EMFILE count: {before_emfile}")

        old = socket.create_connection((args.echo_host, args.echo_port), timeout=2)
        assert_echo(old, b"old-connection-before-fault\n")
        print("[3/8] Old connection established and echoed baseline payload")

        exhaust = api_call(args.api_base, "POST", "/api/v1/drill/exhaust")
        print(
            "[4/8] Sample files filled descriptor table: "
            f"open_sample_files={exhaust['open_sample_files']}, "
            f"open_fd_count={exhaust['open_fd_count']}, "
            f"table_full={exhaust['fd_table_full']}"
        )
        if not exhaust["fd_table_full"]:
            raise AssertionError(f"descriptor table was not full: {exhaust}")

        assert_echo(old, b"old-connection-during-emfile\n")
        print("[5/8] Old connection still echoes while accept returns EMFILE")

        outcome, elapsed = probe_eof(args.echo_host, args.echo_port, timeout=1.0)
        print(f"[6/8] Single new probe got {outcome} {elapsed * 1000:.1f} ms after connect")
        if elapsed > 1.0:
            raise AssertionError(f"EOF/RST took {elapsed:.3f}s, over one second")

        metric = wait_emfile_increment(args.api_base, before_emfile)
        print(
            "[7/8] API observed real accept EMFILE: "
            f"emfile_total={metric['emfile_total']} "
            f"(+{int(metric['emfile_total']) - before_emfile}), "
            f"reaped={metric['reaped_after_emfile_total']}"
        )

        if not args.skip_cpu_check:
            cpu = api_call(
                args.api_base,
                "GET",
                "/api/v1/drill/accept-loop-check?seconds=1.0",
            )
            print(
                "      accept-loop CPU sample: "
                f"cpu_seconds={cpu['cpu_seconds']}, state_after={cpu['state_after']}, "
                f"wchan={cpu['wchan']!r}, busy_suspected={cpu['busy_spin_suspected']}"
            )
            if cpu["busy_spin_suspected"]:
                raise AssertionError(f"accept loop appears to busy-wait: {cpu}")

        release = api_call(args.api_base, "POST", "/api/v1/drill/release")
        released_by_finally = True
        print(f"[8/8] Released {release['closed_sample_files']} sample descriptors via API")

        payload = f"new-connection-after-release-{time.time()}".encode()
        successful_echo_connection(args.echo_host, args.echo_port, payload)
        print("      New connection echoed successfully; listener auto-recovered")
        print("DEMO PASSED")
        return 0

    finally:
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        if not args.keep_fault and not released_by_finally:
            try:
                api_call(args.api_base, "POST", "/api/v1/drill/release")
                print("cleanup: released sample descriptors")
            except Exception as exc:
                print(f"cleanup warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
