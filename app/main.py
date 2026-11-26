"""FastAPI local drill API for Linux TCP echo EMFILE behavior."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .controller import EchoController, EchoControllerError
from .probes import run_tcp_probe
from .selftest import run_self_test

LOG = logging.getLogger("fd_drill.api")

ECHO_BIND_HOST = os.getenv("DRILL_ECHO_BIND_HOST", "0.0.0.0")
ECHO_PORT = int(os.getenv("DRILL_ECHO_PORT", "9000"))
NOFILE_LIMIT = int(os.getenv("DRILL_NOFILE_LIMIT", "32"))
SAMPLE_DIR = os.getenv("DRILL_SAMPLE_DIR", "/tmp/fd-emfile-drill/sample-logs")
# Where the API's probe client should connect.  It is usually loopback because
# the API spawns the echo worker in the same container/process namespace.
PROBE_HOST = os.getenv("DRILL_PROBE_HOST", "127.0.0.1")
# Hostname/port shown to operators for requests run from outside Compose.
ADVERTISED_ECHO_HOST = os.getenv("DRILL_ECHO_ADVERTISED_HOST", "127.0.0.1")
ADVERTISED_ECHO_PORT = int(os.getenv("DRILL_ECHO_ADVERTISED_PORT", str(ECHO_PORT)))


class OpenSamplesRequest(BaseModel):
    count: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="Open this many additional retained sample log descriptors.",
    )
    fill_to_limit: bool = Field(
        default=False,
        description="Continue opening sample files until open(2) returns EMFILE.",
    )
    file_size_bytes: int | None = Field(default=None, ge=0, le=1_000_000)


class ProbeRequest(BaseModel):
    host: str | None = Field(default=None, description="Defaults to in-container loopback.")
    port: int | None = Field(default=None, ge=1, le=65535)
    payload: str = Field(default="drill-probe", max_length=4096)
    send_payload: bool = False
    expect_echo: bool = False
    timeout_seconds: float = Field(default=1.0, gt=0.01, le=5.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    controller = EchoController(
        host=ECHO_BIND_HOST,
        port=ECHO_PORT,
        sample_dir=SAMPLE_DIR,
        nofile_limit=NOFILE_LIMIT,
    )
    app.state.controller = controller
    try:
        controller.start()
    except Exception as exc:  # noqa: BLE001 - keep API up so /healthz can report
        LOG.exception("echo worker failed to start")
        app.state.startup_error = repr(exc)
    else:
        app.state.startup_error = None
    try:
        yield
    finally:
        controller.stop()


app = FastAPI(
    title="FD/EMFILE Sampling Gateway Drill API",
    version="1.0.0",
    description=(
        "Controls a low-RLIMIT_NOFILE TCP echo process. Fill its descriptor "
        "table with retained sample logs to force real accept(2) EMFILE errors, "
        "verify old connections still echo, probe EOF/RST behavior, inspect "
        "the actual EMFILE counter, then release descriptors for recovery."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def controller_or_503(request: Request) -> EchoController:
    controller: EchoController = request.app.state.controller
    if not controller.is_alive():
        raise HTTPException(
            status_code=503,
            detail={
                "message": "echo process is not running",
                "start_error": getattr(request.app.state, "startup_error", None)
                or controller.start_error,
            },
        )
    return controller


def worker_call(controller: EchoController, command: str, **payload: Any) -> dict[str, Any]:
    try:
        return controller.request(command, payload or None)
    except EchoControllerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "fd-emfile-drill",
        "docs": "/docs",
        "health": "/healthz",
        "demo_order": [
            "POST /api/v1/drill/self-test",
            "GET /api/v1/status",
            "POST /api/v1/drill/exhaust",
            "POST /api/v1/probe",
            "GET /api/v1/metrics/emfile",
            "POST /api/v1/drill/release",
            "POST /api/v1/probe with expect_echo=true",
        ],
        "echo_endpoint_from_host": f"{ADVERTISED_ECHO_HOST}:{ADVERTISED_ECHO_PORT}",
    }


@app.get("/healthz", tags=["meta"])
def healthz(request: Request) -> dict[str, Any]:
    controller: EchoController = request.app.state.controller
    alive = controller.is_alive()
    return {
        "ok": alive,
        "api": "ok",
        "echo_process": "running" if alive else "stopped",
        "echo_pid": controller.pid,
        "start_error": getattr(request.app.state, "startup_error", None),
    }


@app.get("/api/v1/status", tags=["drill"])
@app.get("/api/status", include_in_schema=False)
def status(request: Request) -> dict[str, Any]:
    controller = controller_or_503(request)
    worker_status = worker_call(controller, "status")
    worker_status["api_probe_target"] = f"{PROBE_HOST}:{ECHO_PORT}"
    worker_status["host_echo_endpoint"] = (
        f"{ADVERTISED_ECHO_HOST}:{ADVERTISED_ECHO_PORT}"
    )
    return worker_status


@app.get("/api/v1/metrics/emfile", tags=["metrics"])
@app.get("/api/emfile-count", include_in_schema=False)
def emfile_metric(request: Request) -> dict[str, Any]:
    controller = controller_or_503(request)
    status = worker_call(controller, "status")
    metrics = status["metrics"]
    return {
        "pid": status["pid"],
        "emfile_total": metrics["emfile_total"],
        "last_emfile_errno": metrics["last_emfile_errno"],
        "last_emfile_at_unix": metrics["last_emfile_at_unix"],
        "seconds_since_last_emfile": metrics["seconds_since_last_emfile"],
        "reaped_after_emfile_total": metrics["reaped_after_emfile_total"],
        "open_sample_files": status["open_sample_files"],
        "fd_table_full": status["fd_table_full"],
    }


@app.post("/api/v1/drill/exhaust", tags=["drill"])
@app.post("/api/drill/exhaust", include_in_schema=False)
@app.post("/api/fill", include_in_schema=False)
def exhaust(request: Request) -> dict[str, Any]:
    """Open sample log files until open(2) reports EMFILE."""
    controller = controller_or_503(request)
    return worker_call(controller, "exhaust")


@app.post("/api/v1/drill/release", tags=["drill"])
@app.post("/api/drill/release", include_in_schema=False)
@app.post("/api/close", include_in_schema=False)
def release(request: Request) -> dict[str, Any]:
    """Close all API-opened sample descriptors; the listener resumes service."""
    controller = controller_or_503(request)
    return worker_call(controller, "release_samples")


@app.post("/api/v1/sample-files/open", tags=["sample-files"])
def open_sample_files(body: OpenSamplesRequest, request: Request) -> dict[str, Any]:
    if body.count is None and not body.fill_to_limit:
        raise HTTPException(
            status_code=422,
            detail="Provide count and/or set fill_to_limit=true.",
        )
    controller = controller_or_503(request)
    payload = body.model_dump(exclude_none=True)
    return worker_call(controller, "open_samples", **payload)


@app.delete("/api/v1/sample-files", tags=["sample-files"])
def close_sample_files(request: Request) -> dict[str, Any]:
    controller = controller_or_503(request)
    return worker_call(controller, "release_samples")


@app.post("/api/v1/drill/restart", tags=["drill"])
def restart(request: Request) -> dict[str, Any]:
    controller: EchoController = request.app.state.controller
    try:
        ready = controller.restart()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=repr(exc)) from exc
    return {"ok": True, "ready": ready}


@app.post("/api/v1/drill/self-test", tags=["drill"])
@app.post("/api/self-test", include_in_schema=False)
def drill_self_test(request: Request) -> dict[str, Any]:
    """Run the complete old-conn/EMFILE/EOF/no-spin/recovery scenario in-process."""
    controller = controller_or_503(request)
    report = run_self_test(
        controller,
        host=PROBE_HOST,
        port=ECHO_PORT,
        cpu_sample_seconds=0.5,
    )
    if not report.get("ok"):
        raise HTTPException(status_code=500, detail=report)
    return report


@app.get("/api/v1/drill/accept-loop-check", tags=["drill"])
def accept_loop_check(
    request: Request,
    seconds: float = Query(default=1.0, ge=0.1, le=5.0),
) -> dict[str, Any]:
    """Sample accept-thread CPU to demonstrate that EMFILE handling does not spin."""
    controller = controller_or_503(request)
    try:
        sample = controller.accept_loop_sample(seconds)
    except (OSError, EchoControllerError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Include the current counter so a caller can confirm the loop is idle.
    current = worker_call(controller, "status")
    sample["emfile_total_now"] = current["metrics"]["emfile_total"]
    sample["open_sample_files"] = current["open_sample_files"]
    return sample


@app.post("/api/v1/probe", tags=["probes"])
@app.post("/api/probe", include_in_schema=False)
def probe(body: ProbeRequest, request: Request) -> dict[str, Any]:
    controller_or_503(request)
    host = body.host or PROBE_HOST
    port = body.port or ECHO_PORT
    return run_tcp_probe(
        host,
        port,
        payload=body.payload,
        send_payload=body.send_payload,
        expect_echo=body.expect_echo,
        timeout_seconds=body.timeout_seconds,
    )
