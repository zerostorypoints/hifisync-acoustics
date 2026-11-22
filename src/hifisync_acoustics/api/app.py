"""The engine's HTTP API under ``/api/v1``; see docs/api.md.

Every route except ``/health`` needs ``Authorization: Bearer <token>``. Errors are
``{"error": {"code", "message"}}``. Tokens never appear in logs or error bodies, and
error messages carry no file system paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
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
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hifisync_acoustics import __version__, commands
from hifisync_acoustics.analysis.calibration import (
    broadband_offset_db,
    node_correction,
    read_minidsp_cal,
)
from hifisync_acoustics.analysis.ir import impulse_response
from hifisync_acoustics.analysis.preflight import preflight
from hifisync_acoustics.analysis.response import frequency_response
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.api import auth
from hifisync_acoustics.commands import SWEEP_LEAD_S
from hifisync_acoustics.messages import public_message
from hifisync_acoustics.sessions import (
    _safe_name,
    add_recording,
    analyse_session,
    create_session,
    export_session,
)
from hifisync_acoustics.store.db import (
    CommandClosed,
    ManagedByHA,
    SessionNotAccepting,
    Store,
)
from hifisync_acoustics.wav import RecordingError, read_wav

log = logging.getLogger(__name__)
access_log = logging.getLogger("hifisync_acoustics.api.access")

API = "/api/v1"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MIN_SWEEP_S, MAX_SWEEP_S, MAX_REPEATS = 0.5, 60.0, 10
MIN_POLL_WAIT_S, MAX_POLL_WAIT_S, DEFAULT_POLL_WAIT_S = 1, 30, 25
EXPIRY_INTERVAL_S = 5.0
MAX_ACK_DETAIL = 200
_TOKEN_PARAMS = {"token", "access_token", "bearer", "authorization", "auth", "api_key",
                 "apikey", "key"}
_TOKEN_SCOPE_KEY = "hifisync.token"
# The shape of ``secrets.token_urlsafe(32)``: 43 characters of the URL-safe alphabet.
_TOKEN_SHAPE = re.compile(r"(?<![\w-])[A-Za-z0-9_-]{43}(?![\w-])")
_NODE_FIELDS = {"esphome_name": str, "firmware": str, "capture": bool, "install_room_id": str,
                "install_x": float, "install_y": float, "install_z": float}
_NODE_SELF_FIELDS = {"esphome_name", "firmware", "capture"}
_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                 405: "method_not_allowed", 409: "conflict", 413: "body_too_large"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def require_role(request: Request, *roles: str) -> None:
    """Raise 403 ``forbidden`` unless the request's token has one of ``roles``."""
    if request.scope[_TOKEN_SCOPE_KEY].role not in roles:
        raise ApiError(403, "forbidden", "this token may not do that")


def require_own_node(request: Request, node_id: str) -> None:
    """For a ``node`` token, raise 403 ``forbidden`` unless ``node_id`` is its own node,
    and record that the node was heard from. Other roles pass untouched."""
    info = request.scope[_TOKEN_SCOPE_KEY]
    if info.role != "node":
        return
    if info.node_id != node_id:
        raise ApiError(403, "forbidden", "this token is bound to another node")
    request.app.state.store.touch_node(node_id)


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
               max_upload_bytes: int = MAX_UPLOAD_BYTES,
               expiry_interval_s: float = EXPIRY_INTERVAL_S) -> Starlette:
    """``analyse_inline`` runs session analysis in the request instead of the worker
    (tests only). ``expiry_interval_s`` is the period of the background command expiry."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")
    # One event per node that long-polls; arming sets it. Pollers read the database only
    # when woken or at the start, and hold no connection while they wait.
    node_events: dict[str, asyncio.Event] = {}
    # Each poll of a node takes the next generation; only the newest poll may take a command,
    # so a poll on a dead connection that the engine cannot detect leaves it for the next one.
    poll_generations: dict[str, int] = {}
    tmp_root = store.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)

    def run_analysis(session_id: str) -> None:
        try:
            analyse_session(store, session_id)
        except ValueError:
            log.info("session %s is already being analysed", session_id)
        except Exception as exc:
            log.error("analysis of session %s failed: %s", session_id, type(exc).__name__)

    async def start_analysis(session_id: str) -> None:
        if analyse_inline:
            await run_in_threadpool(run_analysis, session_id)
        else:
            executor.submit(run_analysis, session_id)

    async def expire_commands() -> None:
        """Expire overdue node commands and analyse the sessions that leaves complete."""
        complete: list[str] = []
        commands.expire(store, on_analysing=complete.append)
        for session_id in complete:
            await start_analysis(session_id)

    def expiring(handler):
        """Session routes run command expiry first, so they never show a stale state."""
        @functools.wraps(handler)
        async def wrapped(request: Request) -> Response:
            await expire_commands()
            return await handler(request)
        return wrapped

    def node_event(node_id: str) -> asyncio.Event:
        return node_events.setdefault(node_id, asyncio.Event())

    def role(request: Request) -> str:
        return request.scope[_TOKEN_SCOPE_KEY].role

    def staff(handler):
        """Routes are closed to ``node`` tokens unless registered without this wrapper."""
        @functools.wraps(handler)
        async def guarded(request: Request) -> Response:
            require_role(request, "app", "ha")
            return await handler(request)
        return guarded

    def room_or_404(room_id: str) -> dict[str, Any]:
        room = store.get_room(room_id)
        if room is None:
            raise ApiError(404, "not_found", "unknown room")
        return room

    def session_view(session: dict[str, Any]) -> dict[str, Any]:
        for p in session["points"]:
            if p.get("failure_detail"):
                p["failure_detail"] = public_message(p["failure_detail"])
        latest = {c["node_id"]: c for c in store.list_commands(session["id"])}
        nodes = []
        for p in session["points"]:
            cmd = latest.get(p["node_id"]) or {}
            nodes.append({"node_id": p["node_id"], "command_state": cmd.get("state"),
                          "detail": public_message(cmd["detail"]) if cmd.get("detail")
                          else None})
        session["nodes"] = nodes
        session["ready"] = bool(session["nodes"]) and all(
            n["command_state"] == "recording" for n in session["nodes"])
        return session

    # health, sweep --------------------------------------------------------

    async def health(request: Request) -> Response:
        return JSONResponse({"version": __version__, "api": 1})

    async def sweep_wav(request: Request) -> Response:
        q = request.query_params
        spec = _sweep_spec(_parse_float(q.get("duration"), "duration", 6.0),
                           _parse_int(q.get("repeats"), "repeats", 1))

        def render() -> bytes:
            # Leading silence gives a recording started before playback its noise-only
            # lead-in (pre-flight needs at least MIN_LEAD_S of it).
            signal = np.concatenate([np.zeros(int(SWEEP_LEAD_S * spec.sample_rate)),
                                     generate_sweep(spec).signal])
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
            **{k: None if body.get(k) is None else _number(body[k], k, positive=True)
               for k in ("width_m", "length_m", "height_m")},
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
        require_own_node(request, node_id)
        body = await _json_object(request)
        unknown = set(body) - set(_NODE_FIELDS)
        if unknown:
            raise ApiError(400, "bad_request", f"unknown fields: {', '.join(sorted(unknown))}")
        if role(request) == "node" and set(body) - _NODE_SELF_FIELDS:
            raise ApiError(403, "forbidden", "a node token may set esphome_name, firmware "
                           "and capture only")
        current = store.get_node(node_id) or {}
        fields = {k: current.get(k) for k in _NODE_FIELDS}
        for k, v in body.items():
            if v is None and k != "esphome_name":
                fields[k] = None
            elif k == "capture":
                if not isinstance(v, bool):
                    raise ApiError(400, "bad_request", "capture must be true, false or null")
                fields[k] = v
            elif _NODE_FIELDS[k] is float:
                fields[k] = _number(v, k)
            else:
                fields[k] = _text(v, k)
        if (role(request) == "node" and current.get("install_source") == "ha"
                and fields["esphome_name"] != current["esphome_name"]):
            # Home Assistant owns the name: the node's own report of it is ignored, so the
            # node still registers and its firmware and capture still update.
            fields["esphome_name"] = current["esphome_name"]
        if fields["esphome_name"] is None:
            raise ApiError(400, "bad_request", "a new node needs esphome_name")
        if fields["install_room_id"] is not None and store.get_room(fields["install_room_id"]) \
                is None:
            raise ApiError(400, "unknown_room", "install_room_id names no room")
        try:
            node = store.upsert_node(node_id, source="ha" if role(request) == "ha" else "app",
                                     **fields)
        except ManagedByHA as exc:
            raise ApiError(409, exc.code, str(exc)) from None
        if role(request) == "node" and not current:
            store.touch_node(node_id)
            node = store.get_node(node_id) or node
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
        require_own_node(request, node_id)
        session = session_or_404(session_id, False)
        if node_id not in {p["node_id"] for p in session["points"]}:
            raise ApiError(404, "not_found", "node is not part of this session")
        declared = request.headers.get("content-length")
        fd, tmp = tempfile.mkstemp(dir=tmp_root, prefix="upload-", suffix=".wav")
        try:
            received = 0
            with os.fdopen(fd, "wb") as out:
                try:
                    async for chunk in request.stream():
                        out.write(chunk)
                        received += len(chunk)
                except ClientDisconnect:
                    raise _incomplete(received, declared) from None
            if declared is not None and declared.isdigit() and received < int(declared):
                raise _incomplete(received, declared)
            try:
                outcome = await run_in_threadpool(add_recording, store, session_id, node_id,
                                                  Path(tmp))
            except KeyError:
                raise ApiError(404, "not_found", "unknown session or node") from None
            except ValueError:  # SessionNotAccepting
                raise ApiError(409, "session_not_accepting",
                               "the session is not accepting recordings") from None
        finally:
            Path(tmp).unlink(missing_ok=True)
        if outcome.session_state == "analysing":
            await start_analysis(session_id)
        out = dataclasses.asdict(outcome)
        if out["detail"]:
            out["detail"] = public_message(out["detail"], {tmp: "recording"})
        return JSONResponse(out)

    async def arm_session(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        raw = await request.body()
        body = await _json_object(request) if raw.strip() else {}
        if set(body) - {"start_window_s"}:
            raise ApiError(400, "bad_request", "the body accepts start_window_s only")
        window = body.get("start_window_s", commands.DEFAULT_START_WINDOW_S)
        window = _number(window, "start_window_s")
        if not 0.0 <= window <= commands.MAX_START_WINDOW_S:
            raise ApiError(400, "bad_request", "start_window_s must be between 0 and "
                           f"{commands.MAX_START_WINDOW_S:g} s")
        try:
            armed = commands.arm(store, session_id, window)
        except KeyError:
            raise ApiError(404, "not_found", "unknown session") from None
        except SessionNotAccepting:
            raise ApiError(409, "session_not_accepting",
                           "the session is not created or is already armed") from None
        except ValueError as exc:
            if str(exc) == "capture_too_long":
                raise ApiError(400, "capture_too_long", "the capture would be longer than "
                               f"{commands.MAX_CAPTURE_S:g} s; shorten the sweep or the "
                               "start window") from None
            raise ApiError(400, "bad_request", public_message(str(exc))) from None
        for cmd in armed:
            node_event(cmd["node_id"]).set()
        return JSONResponse(session_view(session_or_404(session_id, False)))

    async def next_command(request: Request) -> Response:
        node_id = request.path_params["node_id"]
        require_role(request, "node")
        require_own_node(request, node_id)
        raw = request.query_params.get("wait")
        try:
            wait = DEFAULT_POLL_WAIT_S if raw is None else int(raw)
        except ValueError:
            wait = -1
        if not MIN_POLL_WAIT_S <= wait <= MAX_POLL_WAIT_S:
            raise ApiError(400, "bad_request", f"wait must be an integer from "
                           f"{MIN_POLL_WAIT_S} to {MAX_POLL_WAIT_S}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        generation = poll_generations.get(node_id, 0) + 1
        poll_generations[node_id] = generation
        # Wake an older poll of the node so it returns now instead of holding its connection.
        node_event(node_id).set()
        while True:
            event = node_event(node_id)
            event.clear()
            await expire_commands()
            # Starlette does not cancel a handler whose client left. A stale poll must not
            # take the command that the node's live poll is waiting for: neither one whose
            # client disconnected nor one that a newer poll of the node has replaced.
            if poll_generations.get(node_id) != generation or await request.is_disconnected():
                return Response(status_code=204)
            cmd = commands.next_for(store, node_id)
            if cmd is not None:
                return JSONResponse(cmd)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return Response(status_code=204)
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                return Response(status_code=204)

    async def ack_command(request: Request) -> Response:
        node_id = request.path_params["node_id"]
        require_role(request, "node")
        require_own_node(request, node_id)
        body = await _json_object(request)
        if set(body) - {"state", "detail"}:
            raise ApiError(400, "bad_request", "the body accepts state and detail only")
        state = body.get("state")
        if state not in ("recording", "failed"):
            raise ApiError(400, "bad_request", 'state must be "recording" or "failed"')
        detail = body.get("detail")
        if detail is not None and (not isinstance(detail, str) or len(detail) > MAX_ACK_DETAIL):
            raise ApiError(400, "bad_request",
                           f"detail must be a string of at most {MAX_ACK_DETAIL} characters")
        await expire_commands()
        try:
            cmd, session_state = commands.ack(store, node_id, request.path_params["command_id"],
                                              state, detail or None)
        except KeyError:
            raise ApiError(404, "not_found", "unknown command") from None
        except CommandClosed:
            raise ApiError(409, "command_closed",
                           "the command is done, failed or expired") from None
        if session_state == "analysing":
            await start_analysis(cmd["session_id"])
        return JSONResponse({"id": cmd["id"], "session_id": cmd["session_id"],
                             "state": cmd["state"]})

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

    async def expiry_loop() -> None:
        while True:
            await asyncio.sleep(expiry_interval_s)
            try:
                await expire_commands()
            except Exception as exc:
                log.error("command expiry failed: %s", type(exc).__name__)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        task = asyncio.create_task(expiry_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            executor.shutdown(wait=True, cancel_futures=True)

    routes = [
        Route(f"{API}/health", health),
        Route(f"{API}/sweep.wav", staff(sweep_wav)),
        Route(f"{API}/rooms", staff(list_rooms)),
        Route(f"{API}/rooms/{{room_id}}", staff(get_room), methods=["GET"]),
        Route(f"{API}/rooms/{{room_id}}", staff(put_room), methods=["PUT"]),
        Route(f"{API}/rooms/{{room_id}}/points", staff(list_points), methods=["GET"]),
        Route(f"{API}/rooms/{{room_id}}/points", staff(add_point), methods=["POST"]),
        Route(f"{API}/nodes", staff(list_nodes)),
        Route(f"{API}/nodes/{{node_id}}", put_node, methods=["PUT"]),
        Route(f"{API}/nodes/{{node_id}}/calibrations", staff(list_calibrations), methods=["GET"]),
        Route(f"{API}/nodes/{{node_id}}/calibrations", staff(add_calibration), methods=["POST"]),
        # Node routes: unwrapped, guarded by require_own_node.
        Route(f"{API}/nodes/{{node_id}}/commands/next", next_command, methods=["GET"]),
        Route(f"{API}/nodes/{{node_id}}/commands/{{command_id}}", ack_command, methods=["POST"]),
        Route(f"{API}/sessions", staff(expiring(post_session)), methods=["POST"]),
        Route(f"{API}/sessions/{{session_id}}", staff(expiring(get_session))),
        Route(f"{API}/sessions/{{session_id}}/arm", staff(expiring(arm_session)),
              methods=["POST"]),
        Route(f"{API}/sessions/{{session_id}}/recordings/{{node_id}}", expiring(put_recording),
              methods=["PUT"]),
        Route(f"{API}/sessions/{{session_id}}/results", staff(expiring(get_results))),
        Route(f"{API}/sessions/{{session_id}}/export", staff(expiring(get_export))),
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


def _incomplete(received: int, declared: str | None) -> ApiError:
    expected = f"{declared} announced" if declared else "the body was cut off"
    return ApiError(400, "incomplete_body",
                    f"the upload ended after {received} bytes ({expected}); nothing was stored")


def _calibrate(paths: dict[str, Path], spec: SweepSpec):
    """Correction curve and 1 kHz offset; both recordings must pass pre-flight first."""
    sweep = generate_sweep(spec)
    frs = {}
    for f in ("node", "reference"):
        rec = read_wav(paths[f])
        pre = preflight(rec, sweep)
        if not pre.ok:
            raise RecordingError(pre.code, f"the {f} recording failed pre-flight: {pre.code}")
        frs[f] = frequency_response(impulse_response(rec, sweep), 1 / 6)
    node_fr, ref_fr = frs["node"], frs["reference"]
    ref_cal = read_minidsp_cal(paths["reference_cal"])
    curve = node_correction(node_fr, ref_fr, ref_cal)
    offset = broadband_offset_db(curve)
    if not (math.isfinite(offset) and np.all(np.isfinite(curve.correction_db))):
        raise RecordingError("no_signal", "the node or reference recording has no usable signal")
    return curve, offset
