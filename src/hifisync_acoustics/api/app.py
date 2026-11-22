"""The engine's HTTP API under ``/api/v1``; see docs/api.md.

Every route except ``/health`` needs ``Authorization: Bearer <token>``. Errors are
``{"error": {"code", "message"}}``. Tokens never appear in logs or error bodies, and
error messages carry no file system paths.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote

import numpy as np
from scipy.io import wavfile
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hifisync_acoustics import __version__
from hifisync_acoustics.analysis.calibration import (
    broadband_offset_db,
    node_correction,
    read_minidsp_cal,
)
from hifisync_acoustics.analysis.ir import impulse_response
from hifisync_acoustics.analysis.response import frequency_response
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.api import auth
from hifisync_acoustics.messages import public_message
from hifisync_acoustics.sessions import (
    _safe_name,
    add_recording,
    analyse_session,
    create_session,
    export_session,
)
from hifisync_acoustics.store.db import ManagedByHA, Store
from hifisync_acoustics.wav import RecordingError, read_wav

log = logging.getLogger(__name__)
access_log = logging.getLogger("hifisync_acoustics.api.access")

API = "/api/v1"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MIN_SWEEP_S, MAX_SWEEP_S, MAX_REPEATS = 0.5, 60.0, 10
_TOKEN_PARAMS = {"token", "access_token", "bearer", "authorization", "auth", "api_key",
                 "apikey", "key"}
_TOKEN_SCOPE_KEY = "hifisync.token"
# The shape of ``secrets.token_urlsafe(32)``: 43 characters of the URL-safe alphabet.
_TOKEN_SHAPE = re.compile(r"(?<![\w-])[A-Za-z0-9_-]{43}(?![\w-])")
_NODE_FIELDS = {"esphome_name": str, "firmware": str, "capture": str, "install_room_id": str,
                "install_x": float, "install_y": float, "install_z": float}
_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 404: "not_found",
                 405: "method_not_allowed", 409: "conflict", 413: "body_too_large"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error_response(status: int, code: str, message: str, headers: dict | None = None):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers=headers)


# middleware ---------------------------------------------------------------


class _AccessLog:
    """Logs method, path (never the query string), status and duration. Token-shaped
    path segments are logged as ``<redacted>``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        status = 500

        async def send_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            path = _TOKEN_SHAPE.sub("<redacted>", scope["path"])
            access_log.info("%s %s %d %.1f ms", scope["method"], path, status,
                            (time.perf_counter() - start) * 1000)


class _BodyLimit:
    """Request bodies over ``limit`` bytes answer 413, whether announced by
    Content-Length or found while streaming."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = None
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = None
        total = 0

        async def limited() -> Message:
            nonlocal total
            if declared is not None and declared > self.limit:
                raise _too_large(self.limit)
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.limit:
                    raise _too_large(self.limit)
            return message

        await self.app(scope, limited, send)


def _too_large(limit: int) -> ApiError:
    return ApiError(413, "body_too_large", f"request body is larger than {limit} bytes")


class _Auth:
    """Rejects tokens in the query string, then requires a valid bearer token."""

    def __init__(self, app: ASGIApp, store: Store) -> None:
        self.app, self.store = app, store

    @staticmethod
    def _token_in_query(query: str) -> bool:
        """A token-named parameter or any token-shaped value, raw or percent-decoded.
        Checked by shape only, so it costs no database access before authentication."""
        for key, _ in parse_qsl(query, keep_blank_values=True):
            if key.lower() in _TOKEN_PARAMS:
                return True
        return any(_TOKEN_SHAPE.search(q) for q in (query, unquote(query)))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self._token_in_query(scope.get("query_string", b"").decode("latin-1")):
            response = _error_response(
                400, "token_in_url",
                "tokens are never accepted in the URL; send Authorization: Bearer <token>")
            return await response(scope, receive, send)
        if scope["path"] != f"{API}/health":
            header = ""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    header = v.decode("latin-1")
            scheme, _, token = header.partition(" ")
            info = auth.lookup(self.store, token.strip()) if scheme.lower() == "bearer" else None
            if info is None:
                response = _error_response(401, "unauthorized", "missing or invalid token",
                                           headers={"WWW-Authenticate": "Bearer"})
                return await response(scope, receive, send)
            scope[_TOKEN_SCOPE_KEY] = info
        await self.app(scope, receive, send)


# validation ----------------------------------------------------------------


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_request", "body is not valid JSON") from None
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "body must be a JSON object")
    return body


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ApiError(400, "bad_request", f"{name} must be a finite number")
    if positive and value <= 0:
        raise ApiError(400, "bad_request", f"{name} must be greater than 0")
    return float(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "bad_request", f"{name} must be a non-empty string")
    return value


def _sweep_spec(duration: Any, repeats: Any) -> SweepSpec:
    duration = _number(duration, "duration")
    if isinstance(repeats, bool) or not isinstance(repeats, int):
        raise ApiError(400, "bad_request", "repeats must be an integer")
    if not MIN_SWEEP_S <= duration <= MAX_SWEEP_S:
        raise ApiError(400, "bad_request",
                       f"duration must be between {MIN_SWEEP_S} and {MAX_SWEEP_S} s")
    if not 1 <= repeats <= MAX_REPEATS:
        raise ApiError(400, "bad_request", f"repeats must be between 1 and {MAX_REPEATS}")
    return SweepSpec(duration_s=duration, repeats=repeats)


def _parse_float(raw: str | None, name: str, default: float) -> float:
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ApiError(400, "bad_request", f"{name} must be a number") from None


def _parse_int(raw: str | None, name: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ApiError(400, "bad_request", f"{name} must be an integer") from None


def _id(value: str, kind: str) -> str:
    if not _safe_name(value) or len(value) > 128:
        raise ApiError(400, "bad_request", f"invalid {kind} id")
    return value


# application ----------------------------------------------------------------


def create_app(store: Store, *, analyse_inline: bool = False,
               max_upload_bytes: int = MAX_UPLOAD_BYTES) -> Starlette:
    """``analyse_inline`` runs session analysis in the request instead of the worker
    (tests only)."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")
    tmp_root = store.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)

    def run_analysis(session_id: str) -> None:
        try:
            analyse_session(store, session_id)
        except ValueError:
            log.info("session %s is already being analysed", session_id)
        except Exception as exc:
            log.error("analysis of session %s failed: %s", session_id, type(exc).__name__)

    def role(request: Request) -> str:
        return request.scope[_TOKEN_SCOPE_KEY].role

    def room_or_404(room_id: str) -> dict[str, Any]:
        room = store.get_room(room_id)
        if room is None:
            raise ApiError(404, "not_found", "unknown room")
        return room

    def session_view(session: dict[str, Any]) -> dict[str, Any]:
        for p in session["points"]:
            if p.get("failure_detail"):
                p["failure_detail"] = public_message(p["failure_detail"])
        return session

    # health, sweep --------------------------------------------------------

    async def health(request: Request) -> Response:
        return JSONResponse({"version": __version__, "api": 1})

    async def sweep_wav(request: Request) -> Response:
        q = request.query_params
        spec = _sweep_spec(_parse_float(q.get("duration"), "duration", 6.0),
                           _parse_int(q.get("repeats"), "repeats", 1))

        def render() -> bytes:
            signal = generate_sweep(spec).signal
            buf = io.BytesIO()
            # Truncation toward zero keeps the file peak at or below the sweep's cap.
            pcm = np.trunc(signal * 32768).astype(np.int16)
            wavfile.write(buf, spec.sample_rate, pcm)
            return buf.getvalue()

        return Response(await run_in_threadpool(render), media_type="audio/wav",
                        headers={"Content-Disposition": 'attachment; filename="sweep.wav"'})

    # rooms and points -----------------------------------------------------

    async def list_rooms(request: Request) -> Response:
        return JSONResponse(store.list_rooms())

    async def get_room(request: Request) -> Response:
        return JSONResponse(room_or_404(request.path_params["room_id"]))

    async def put_room(request: Request) -> Response:
        room_id = _id(request.path_params["room_id"], "room")
        body = await _json_object(request)
        room = store.upsert_room(
            room_id, _text(body.get("name"), "name"),
            width_m=_number(body.get("width_m"), "width_m", positive=True),
            length_m=_number(body.get("length_m"), "length_m", positive=True),
            height_m=_number(body.get("height_m"), "height_m", positive=True),
            snapshot=body.get("snapshot"))
        return JSONResponse(room)

    async def list_points(request: Request) -> Response:
        room = room_or_404(request.path_params["room_id"])
        return JSONResponse(store.list_points(room["id"]))

    async def add_point(request: Request) -> Response:
        room = room_or_404(request.path_params["room_id"])
        body = await _json_object(request)
        label = _text(body.get("label"), "label")
        x, y, z = (_number(body.get(k), k) for k in ("x", "y", "z"))
        point_id = store.add_point(room["id"], label, x, y, z)
        return JSONResponse({"id": point_id, "room_id": room["id"], "label": label,
                             "x": x, "y": y, "z": z}, status_code=201)

    # nodes and calibrations -----------------------------------------------

    async def list_nodes(request: Request) -> Response:
        return JSONResponse(store.list_nodes())

    async def put_node(request: Request) -> Response:
        node_id = _id(request.path_params["node_id"], "node")
        body = await _json_object(request)
        unknown = set(body) - set(_NODE_FIELDS)
        if unknown:
            raise ApiError(400, "bad_request", f"unknown fields: {', '.join(sorted(unknown))}")
        current = store.get_node(node_id) or {}
        fields = {k: current.get(k) for k in _NODE_FIELDS}
        for k, v in body.items():
            if v is None and k != "esphome_name":
                fields[k] = None
            elif _NODE_FIELDS[k] is float:
                fields[k] = _number(v, k)
            else:
                fields[k] = _text(v, k)
        if fields["esphome_name"] is None:
            raise ApiError(400, "bad_request", "a new node needs esphome_name")
        if fields["install_room_id"] is not None and store.get_room(fields["install_room_id"]) \
                is None:
            raise ApiError(400, "unknown_room", "install_room_id names no room")
        try:
            node = store.upsert_node(node_id, source=role(request), **fields)
        except ManagedByHA as exc:
            raise ApiError(409, exc.code, str(exc)) from None
        return JSONResponse(node)

    def node_or_404(node_id: str) -> dict[str, Any]:
        node = store.get_node(node_id)
        if node is None:
            raise ApiError(404, "not_found", "unknown node")
        return node

    async def list_calibrations(request: Request) -> Response:
        node = node_or_404(request.path_params["node_id"])
        return JSONResponse([{
            "version": c.version, "created_at": c.created_at, "reference_id": c.reference_id,
            "reference_cal_name": c.reference_cal_name, "offset_db": c.offset_db,
            "conditions": c.conditions,
            "curve": {"freqs_hz": c.curve.freqs_hz.tolist(),
                      "correction_db": c.curve.correction_db.tolist(),
                      "sensitivity_db": c.curve.sensitivity_db},
        } for c in store.list_calibrations(node["id"])])

    async def add_calibration(request: Request) -> Response:
        node = node_or_404(request.path_params["node_id"])
        async with request.form(max_files=3, max_fields=10, max_part_size=64 * 1024) as form:
            uploads = {}
            for field in ("node", "reference", "reference_cal"):
                part = form.get(field)
                if not isinstance(part, UploadFile):
                    raise ApiError(400, "bad_request", f"{field} file is missing")
                uploads[field] = part

            def text_field(name: str) -> str | None:
                v = form.get(name)
                return v if isinstance(v, str) and v != "" else None

            spec = _sweep_spec(_parse_float(text_field("duration"), "duration", 6.0),
                               _parse_int(text_field("repeats"), "repeats", 1))
            cal_name = uploads["reference_cal"].filename
            cal_name = re.split(r"[/\\]", cal_name)[-1][:255] if cal_name else None
            with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
                paths = {f: Path(tmp) / f for f in uploads}
                for f, part in uploads.items():
                    with open(paths[f], "wb") as out:
                        await run_in_threadpool(shutil.copyfileobj, part.file, out)
                try:
                    curve, offset = await run_in_threadpool(_calibrate, paths, spec)
                except RecordingError as exc:
                    labels = {str(p): f for f, p in paths.items()}
                    raise ApiError(400, exc.code, public_message(str(exc), labels)) from None
        version = store.add_calibration(node["id"], curve, offset, text_field("reference_id"),
                                        cal_name, text_field("conditions"))
        return JSONResponse({"version": version, "offset_db": offset}, status_code=201)

    # sessions -------------------------------------------------------------

    async def post_session(request: Request) -> Response:
        body = await _json_object(request)
        room_id = _text(body.get("room_id"), "room_id")
        raw = body.get("assignments")
        if not isinstance(raw, list) or not all(isinstance(a, dict) for a in raw):
            raise ApiError(400, "bad_request", "assignments must be a list of objects")
        assignments = [(_text(a.get("point_id"), "point_id"), _text(a.get("node_id"), "node_id"))
                       for a in raw]
        sweep = body.get("sweep") or {}
        if not isinstance(sweep, dict) or set(sweep) - {"duration_s", "repeats"}:
            raise ApiError(400, "bad_request", "sweep accepts duration_s and repeats only")
        spec = _sweep_spec(sweep.get("duration_s", 6.0), sweep.get("repeats", 1))
        chain = body.get("player_chain")
        if chain is not None and not isinstance(chain, str):
            raise ApiError(400, "bad_request", "player_chain must be a string")
        try:
            session_id = create_session(store, room_id, assignments, spec, chain)
        except ValueError as exc:
            raise ApiError(400, "bad_request", public_message(str(exc))) from None
        return JSONResponse(session_view(store.get_session(session_id, include_results=False)),
                            status_code=201)

    def session_or_404(session_id: str, include_results: bool) -> dict[str, Any]:
        session = store.get_session(session_id, include_results=include_results)
        if session is None:
            raise ApiError(404, "not_found", "unknown session")
        return session

    async def get_session(request: Request) -> Response:
        return JSONResponse(session_view(session_or_404(request.path_params["session_id"],
                                                        False)))

    async def get_results(request: Request) -> Response:
        s = session_view(session_or_404(request.path_params["session_id"], True))
        return JSONResponse({
            "session_id": s["id"], "state": s["state"], "failure": s["failure"],
            "points": [{k: p[k] for k in ("point_id", "node_id", "state", "failure",
                                          "failure_detail", "result")} for p in s["points"]],
        })

    async def put_recording(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        node_id = request.path_params["node_id"]
        session = session_or_404(session_id, False)
        if node_id not in {p["node_id"] for p in session["points"]}:
            raise ApiError(404, "not_found", "node is not part of this session")
        fd, tmp = tempfile.mkstemp(dir=tmp_root, prefix="upload-", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    out.write(chunk)
            try:
                outcome = await run_in_threadpool(add_recording, store, session_id, node_id,
                                                  Path(tmp))
            except KeyError:
                raise ApiError(404, "not_found", "unknown session or node") from None
            except ValueError:
                raise ApiError(409, "session_not_accepting",
                               "the session is not accepting recordings") from None
        finally:
            Path(tmp).unlink(missing_ok=True)
        if outcome.session_state == "analysing":
            if analyse_inline:
                await run_in_threadpool(run_analysis, session_id)
            else:
                executor.submit(run_analysis, session_id)
        out = dataclasses.asdict(outcome)
        if out["detail"]:
            out["detail"] = public_message(out["detail"], {tmp: "recording"})
        return JSONResponse(out)

    async def get_export(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        try:
            path = await run_in_threadpool(export_session, store, session_id)
        except KeyError:
            raise ApiError(404, "not_found", "unknown session") from None
        return FileResponse(path, media_type="application/zip",
                            filename=f"session-{session_id}.zip")

    # errors ---------------------------------------------------------------

    async def api_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, ApiError)
        return _error_response(exc.status, exc.code, exc.message)

    async def http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)
        code = _STATUS_CODES.get(exc.status_code, "error")
        message = public_message(str(exc.detail)) if exc.status_code == 400 else code
        return _error_response(exc.status_code, code, message)

    async def server_error(request: Request, exc: Exception) -> Response:
        log.error("%s %s failed: %s", request.method, request.url.path, type(exc).__name__)
        return _error_response(500, "internal_error", "internal error")

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        executor.shutdown(wait=True, cancel_futures=True)

    routes = [
        Route(f"{API}/health", health),
        Route(f"{API}/sweep.wav", sweep_wav),
        Route(f"{API}/rooms", list_rooms),
        Route(f"{API}/rooms/{{room_id}}", get_room, methods=["GET"]),
        Route(f"{API}/rooms/{{room_id}}", put_room, methods=["PUT"]),
        Route(f"{API}/rooms/{{room_id}}/points", list_points, methods=["GET"]),
        Route(f"{API}/rooms/{{room_id}}/points", add_point, methods=["POST"]),
        Route(f"{API}/nodes", list_nodes),
        Route(f"{API}/nodes/{{node_id}}", put_node, methods=["PUT"]),
        Route(f"{API}/nodes/{{node_id}}/calibrations", list_calibrations, methods=["GET"]),
        Route(f"{API}/nodes/{{node_id}}/calibrations", add_calibration, methods=["POST"]),
        Route(f"{API}/sessions", post_session, methods=["POST"]),
        Route(f"{API}/sessions/{{session_id}}", get_session),
        Route(f"{API}/sessions/{{session_id}}/recordings/{{node_id}}", put_recording,
              methods=["PUT"]),
        Route(f"{API}/sessions/{{session_id}}/results", get_results),
        Route(f"{API}/sessions/{{session_id}}/export", get_export),
    ]
    app = Starlette(
        routes=routes,
        middleware=[Middleware(_AccessLog), Middleware(_BodyLimit, limit=max_upload_bytes),
                    Middleware(_Auth, store=store)],
        exception_handlers={ApiError: api_error, HTTPException: http_error,
                            Exception: server_error},
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.executor = executor
    return app


def _calibrate(paths: dict[str, Path], spec: SweepSpec):
    sweep = generate_sweep(spec)
    node_fr, ref_fr = (frequency_response(impulse_response(read_wav(paths[f]), sweep), 1 / 6)
                       for f in ("node", "reference"))
    ref_cal = read_minidsp_cal(paths["reference_cal"])
    curve = node_correction(node_fr, ref_fr, ref_cal)
    offset = broadband_offset_db(curve)
    if not (math.isfinite(offset) and np.all(np.isfinite(curve.correction_db))):
        raise RecordingError("no_signal", "the node or reference recording has no usable signal")
    return curve, offset
