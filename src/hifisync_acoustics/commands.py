"""Arming a session and the command queue that measurement nodes long-poll.

Arming creates one ``record`` command per node of a ``created`` session and moves the
session to ``recording``. A node takes its command (``taken``), acks ``recording`` before it
stores the first sample, and uploads its WAV, which makes the command ``done``. A node that
cannot record acks ``failed`` and its point fails ``node_failed``.

Command states: pending -> taken -> recording -> done, or failed, or expired. A command
expires, and its point fails ``node_offline`` with the stage in the detail, when it is

- not taken within ``COMMAND_TAKE_TIMEOUT_S`` of arming (``never polled``);
- taken but not acked ``recording`` within ``COMMAND_ACK_TIMEOUT_S`` (``no recording ack``);
- not uploaded within ``capture_s + COMMAND_UPLOAD_GRACE_S`` of being taken (``no upload``).

Points leave ``pending`` through the same store path as uploads, so the session finishes
or fails by the usual pending/uploaded counting.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from hifisync_acoustics.analysis.sweep import SweepSpec
from hifisync_acoustics.store.db import CommandClosed, SessionNotAccepting, Store

__all__ = ["CommandClosed", "SessionNotAccepting", "ack", "arm", "capture_s", "expire",
           "next_for", "sweep_file_s"]

SWEEP_LEAD_S = 1.0  # digital silence before the sweep in sweep.wav
MAX_CAPTURE_S = 60.0
CAPTURE_TAIL_S = 3.0
DEFAULT_START_WINDOW_S = 10.0
MAX_START_WINDOW_S = 30.0
COMMAND_TAKE_TIMEOUT_S = 30.0
COMMAND_ACK_TIMEOUT_S = 10.0
COMMAND_UPLOAD_GRACE_S = 60.0
API_PREFIX = "/api/v1"


def sweep_file_s(spec: SweepSpec) -> float:
    """Length of ``GET /sweep.wav`` for ``spec``: lead-in, sweeps and the gaps between them."""
    return SWEEP_LEAD_S + spec.repeats * spec.duration_s + (spec.repeats - 1) * spec.gap_s


def capture_s(spec: SweepSpec, start_window_s: float) -> float:
    """How long a node records: the window to start playback, the file, and a tail."""
    return start_window_s + sweep_file_s(spec) + CAPTURE_TAIL_S


def arm(store: Store, session_id: str,
        start_window_s: float = DEFAULT_START_WINDOW_S) -> list[dict[str, Any]]:
    """Create one pending ``record`` command per node of the session and move it to
    ``recording``. Raises KeyError for an unknown session, ValueError("capture_too_long")
    when the capture is over ``MAX_CAPTURE_S``, ValueError for a start window outside
    0..``MAX_START_WINDOW_S`` and SessionNotAccepting when the session is not ``created``
    or is already armed."""
    if not 0.0 <= start_window_s <= MAX_START_WINDOW_S:
        raise ValueError(f"start_window_s must be between 0 and {MAX_START_WINDOW_S:g} s")
    session = store.get_session(session_id, include_results=False)
    if session is None:
        raise KeyError(session_id)
    capture = capture_s(SweepSpec(**session["sweep"]), start_window_s)
    if capture > MAX_CAPTURE_S:
        raise ValueError("capture_too_long")
    payload = {"capture_s": capture, "start_window_s": float(start_window_s)}
    return store.arm_session(session_id, [(str(uuid.uuid4()), p["node_id"], payload)
                                          for p in session["points"]])


def next_for(store: Store, node_id: str) -> dict[str, Any] | None:
    """Take the node's oldest pending command: the poll answer, or None."""
    cmd = store.take_command(node_id)
    if cmd is None:
        return None
    return {"id": cmd["id"], "kind": cmd["kind"], "session_id": cmd["session_id"],
            "capture_s": cmd["payload"]["capture_s"],
            "upload_path": f"{API_PREFIX}/sessions/{cmd['session_id']}/recordings/{node_id}"}


def ack(store: Store, node_id: str, command_id: str, state: str,
        detail: str | None = None) -> tuple[dict[str, Any], str | None]:
    """The node's ack, ``recording`` or ``failed``. Returns the command and, for ``failed``,
    the session state after its point failed (``analysing`` means analysis should start).
    Raises KeyError for a command of another node, CommandClosed for a closed one and
    ValueError for another state."""
    return store.ack_command(node_id, command_id, state, detail)


def _deadline(cmd: dict[str, Any]) -> tuple[datetime, str]:
    if cmd["state"] == "pending":
        return (datetime.fromisoformat(cmd["created_at"])
                + timedelta(seconds=COMMAND_TAKE_TIMEOUT_S), "never polled")
    taken = datetime.fromisoformat(cmd["taken_at"])
    if cmd["state"] == "taken":
        return taken + timedelta(seconds=COMMAND_ACK_TIMEOUT_S), "no recording ack"
    grace = cmd["payload"]["capture_s"] + COMMAND_UPLOAD_GRACE_S
    return taken + timedelta(seconds=grace), "no upload"


def expire(store: Store, now: datetime | None = None,
           on_analysing: Callable[[str], None] | None = None) -> int:
    """Expire every open command past its deadline at ``now`` (default: the clock) and fail
    its point ``node_offline``. ``on_analysing`` is called with each session the expiry left
    with no pending point and something to analyse. Returns the number expired."""
    now = now or datetime.now(UTC)
    expired = 0
    for cmd in store.open_commands():
        deadline, stage = _deadline(cmd)
        if now <= deadline:
            continue
        done, session_state = store.expire_command(
            cmd["id"], cmd["state"], stage, f"the node did not answer in time: {stage}")
        if done:
            expired += 1
            if session_state == "analysing" and on_analysing is not None:
                on_analysing(cmd["session_id"])
    return expired
