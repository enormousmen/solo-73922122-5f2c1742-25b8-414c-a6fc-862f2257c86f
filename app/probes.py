"""TCP probes used by the drill API and demo clients."""

from __future__ import annotations

import errno
import socket
import time
from typing import Any


def run_tcp_probe(
    host: str,
    port: int,
    *,
    payload: str = "drill-probe",
    send_payload: bool = False,
    expect_echo: bool = False,
    timeout_seconds: float = 1.0,
) -> dict[str, Any]:
    """Open one TCP connection and classify EOF/RST/echo/timeout.

    Timing begins immediately after connect(2) succeeds.  During EMFILE drill
    mode the worker accepts via its rescue descriptor and closes the socket, so
    a no-payload probe normally receives EOF in well under one second.
    """
    if expect_echo:
        send_payload = True

    data = payload.encode("utf-8")
    started = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=timeout_seconds)
    except OSError as exc:
        return {
            "connected": False,
            "outcome": "connect_error",
            "errno": exc.errno,
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    with sock:
        connected_at = time.perf_counter()
        connect_ms = (connected_at - started) * 1000
        sock.settimeout(timeout_seconds)
        sent_bytes = 0

        try:
            if send_payload:
                sock.sendall(data)
                sent_bytes = len(data)

            if expect_echo:
                chunks: list[bytes] = []
                remaining = len(data)
                while remaining > 0:
                    chunk = sock.recv(min(65536, remaining))
                    if not chunk:
                        elapsed_ms = (time.perf_counter() - connected_at) * 1000
                        return {
                            "connected": True,
                            "outcome": "eof_before_full_echo",
                            "connect_ms": round(connect_ms, 3),
                            "elapsed_after_connect_ms": round(elapsed_ms, 3),
                            "within_one_second": elapsed_ms <= 1000.0,
                            "sent_bytes": sent_bytes,
                            "received_bytes": sum(map(len, chunks)),
                        }
                    chunks.append(chunk)
                    remaining -= len(chunk)

                received = b"".join(chunks)
                elapsed_ms = (time.perf_counter() - connected_at) * 1000
                return {
                    "connected": True,
                    "outcome": "echo" if received == data else "echo_mismatch",
                    "connect_ms": round(connect_ms, 3),
                    "elapsed_after_connect_ms": round(elapsed_ms, 3),
                    "within_one_second": elapsed_ms <= 1000.0,
                    "sent_bytes": sent_bytes,
                    "received_bytes": len(received),
                    "echo_matches": received == data,
                }

            first = sock.recv(4096)
            elapsed_ms = (time.perf_counter() - connected_at) * 1000
            if first == b"":
                outcome = "eof"
            else:
                outcome = "data"
            return {
                "connected": True,
                "outcome": outcome,
                "connect_ms": round(connect_ms, 3),
                "elapsed_after_connect_ms": round(elapsed_ms, 3),
                "within_one_second": elapsed_ms <= 1000.0,
                "sent_bytes": sent_bytes,
                "received_bytes": len(first),
                "preview": first[:128].decode("utf-8", errors="replace"),
            }

        except socket.timeout:
            elapsed_ms = (time.perf_counter() - connected_at) * 1000
            return {
                "connected": True,
                "outcome": "timeout",
                "connect_ms": round(connect_ms, 3),
                "elapsed_after_connect_ms": round(elapsed_ms, 3),
                "within_one_second": elapsed_ms <= 1000.0,
                "sent_bytes": sent_bytes,
            }
        except ConnectionResetError as exc:
            elapsed_ms = (time.perf_counter() - connected_at) * 1000
            return {
                "connected": True,
                "outcome": "reset",
                "errno": exc.errno,
                "connect_ms": round(connect_ms, 3),
                "elapsed_after_connect_ms": round(elapsed_ms, 3),
                "within_one_second": elapsed_ms <= 1000.0,
                "sent_bytes": sent_bytes,
            }
        except OSError as exc:
            elapsed_ms = (time.perf_counter() - connected_at) * 1000
            outcome = "reset" if exc.errno in (errno.ECONNRESET, errno.EPIPE) else "io_error"
            return {
                "connected": True,
                "outcome": outcome,
                "errno": exc.errno,
                "error": str(exc),
                "connect_ms": round(connect_ms, 3),
                "elapsed_after_connect_ms": round(elapsed_ms, 3),
                "within_one_second": elapsed_ms <= 1000.0,
                "sent_bytes": sent_bytes,
            }
