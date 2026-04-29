"""Persistent store for the GEO program-audit agent.

Data is written to disk under  data/{audit_id}/  so that server restarts
don't lose in-flight or completed audits.  In-memory dicts act as a
write-through cache so hot paths (SSE log streaming) never touch disk.

Layout
------
data/{audit_id}/
    audit_input.json       – Pydantic ProgramAuditInput serialised with model_dump
    brief.json             – {"sections": {}, "human_review_flags": [], ...}
    raw_data.json          – {"fetched_urls": {}, "self_assessments": [], ...}
    status.txt             – one of: processing / complete / error / unknown
    review_responses.json  – {flag_index: {input, status, ...}}
    log.json               – [{timestamp, message, level, details}, ...]

Human-input events are intentionally ephemeral (in-memory only) – they
are only needed while the agent is live.  Log history is persisted to
disk when the agent completes so historical audits can display it.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from app.models import ProgramAuditInput, LogEntry

# ---------------------------------------------------------------------------
# Storage root
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)


def _app_dir(app_id: str) -> Path:
    d = _DATA_DIR / app_id
    d.mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# In-memory caches (populated on first access, written through on every save)
# ---------------------------------------------------------------------------

_audit_inputs: dict[str, ProgramAuditInput] = {}
_briefs: dict[str, dict] = {}
_raw_data: dict[str, dict] = {}
_log_history: dict[str, list[LogEntry]] = {}
_status: dict[str, str] = {}
_review_responses: dict[str, dict[int, dict]] = {}


# ---------------------------------------------------------------------------
# Audit inputs (the program-name + recipient-hint the user submitted)
# ---------------------------------------------------------------------------

def save_audit_input(audit_id: str, audit_input: ProgramAuditInput) -> None:
    _audit_inputs[audit_id] = audit_input
    path = _app_dir(audit_id) / "audit_input.json"
    path.write_text(audit_input.model_dump_json(indent=2), encoding="utf-8")


def get_audit_input(audit_id: str) -> ProgramAuditInput | None:
    if audit_id in _audit_inputs:
        return _audit_inputs[audit_id]
    path = _DATA_DIR / audit_id / "audit_input.json"
    if path.exists():
        try:
            ai = ProgramAuditInput.model_validate_json(path.read_text(encoding="utf-8"))
            _audit_inputs[audit_id] = ai
            return ai
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Log history  (in-memory while live, persisted to disk on completion)
# ---------------------------------------------------------------------------

def init_log(app_id: str) -> None:
    _log_history[app_id] = []
    # Don't override an on-disk status that might already be "complete" / "error"
    # (e.g. if a page is refreshed mid-flight before the worker finishes)
    if app_id not in _status:
        _status[app_id] = "processing"
        _write_status(app_id, "processing")


def get_log_history(app_id: str) -> list[LogEntry]:
    if app_id in _log_history:
        return _log_history[app_id]
    # Not in memory; try loading from disk (historical brief)
    return _load_log_history(app_id)


def _load_log_history(app_id: str) -> list[LogEntry]:
    path = _DATA_DIR / app_id / "log.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = [LogEntry.model_validate(item) for item in raw]
            _log_history[app_id] = entries
            return entries
        except Exception:
            pass
    return []


def save_log_history(app_id: str) -> None:
    """Persist log entries to disk so they survive server restarts."""
    entries = _log_history.get(app_id, [])
    if not entries:
        return
    path = _app_dir(app_id) / "log.json"
    serialised = [entry.model_dump(mode="json") for entry in entries]
    path.write_text(json.dumps(serialised, indent=2, default=str), encoding="utf-8")


async def emit_log(app_id: str, entry: LogEntry) -> None:
    if app_id in _log_history:
        _log_history[app_id].append(entry)


def append_log_entry(app_id: str, entry: LogEntry) -> None:
    """Append a log entry and persist to disk immediately.

    Unlike emit_log (used during a live agent run), this works after
    the run has completed; it loads from disk if necessary, appends,
    and re-persists.  Used by the review endpoint so that reviewer
    interactions appear in the historical Agent Log.
    """
    if app_id not in _log_history:
        _load_log_history(app_id)
    _log_history.setdefault(app_id, []).append(entry)
    save_log_history(app_id)


# ---------------------------------------------------------------------------
# Audit state (structured): goal_anchor, lenses, drafts, synthesis,
# reasoning_trail, metadata. Mutated in-place by the MCP tools while the
# agent runs; persisted to audit.json on completion or error.
# ---------------------------------------------------------------------------

def empty_audit_state() -> dict:
    return {
        "goal_anchor": None,
        "lenses": {},      # lens_key -> Lens dict
        "drafts": [],      # list of AccountabilityDraft dicts
        "synthesis": None,
        "reasoning_trail": [],
        "flags": [],       # list of human_review flag dicts
        "metadata": {},
    }


def register_live_audit_state(app_id: str, state: dict) -> None:
    """Put the agent's mutable audit_state into the in-memory cache so the
    /stream endpoint can poll it during the run. The same dict is later
    persisted to disk by save_audit_state."""
    _briefs[app_id] = state


def save_audit_state(app_id: str, state: dict) -> None:
    _briefs[app_id] = state
    path = _app_dir(app_id) / "audit.json"
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def get_audit_state(app_id: str) -> dict | None:
    if app_id in _briefs:
        return _briefs[app_id]
    path = _DATA_DIR / app_id / "audit.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _briefs[app_id] = data
            return data
        except Exception:
            return None
    return None


# Back-compat aliases used by older paths; treat audit_state as the brief.
def save_brief(app_id: str, brief: dict) -> None:
    save_audit_state(app_id, brief)


def get_brief(app_id: str) -> dict | None:
    return get_audit_state(app_id)


# ---------------------------------------------------------------------------
# Raw data
# ---------------------------------------------------------------------------

def save_raw_data(app_id: str, data: dict) -> None:
    _raw_data[app_id] = data
    path = _app_dir(app_id) / "raw_data.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def get_raw_data(app_id: str) -> dict | None:
    if app_id in _raw_data:
        return _raw_data[app_id]
    path = _DATA_DIR / app_id / "raw_data.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _raw_data[app_id] = data
            return data
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _write_status(app_id: str, status: str) -> None:
    (_app_dir(app_id) / "status.txt").write_text(status, encoding="utf-8")


def set_status(app_id: str, status: str) -> None:
    _status[app_id] = status
    _write_status(app_id, status)


def get_status(app_id: str) -> str:
    if app_id in _status:
        return _status[app_id]
    path = _DATA_DIR / app_id / "status.txt"
    if path.exists():
        status = path.read_text(encoding="utf-8").strip()
        _status[app_id] = status
        return status
    return "unknown"


# ---------------------------------------------------------------------------
# Review responses
# ---------------------------------------------------------------------------

def save_review_response(app_id: str, flag_index: int, response: dict) -> None:
    if app_id not in _review_responses:
        _review_responses[app_id] = {}
    _review_responses[app_id][flag_index] = response
    path = _app_dir(app_id) / "review_responses.json"
    # Serialise with string keys for JSON compatibility, restore as int on read
    serialisable = {str(k): v for k, v in _review_responses[app_id].items()}
    path.write_text(json.dumps(serialisable, indent=2, default=str), encoding="utf-8")


def get_review_responses(app_id: str) -> dict[int, dict]:
    if app_id in _review_responses:
        return _review_responses[app_id]
    return _load_review_responses(app_id)


def get_review_response(app_id: str, flag_index: int) -> dict | None:
    if app_id not in _review_responses:
        _load_review_responses(app_id)
    return _review_responses.get(app_id, {}).get(flag_index)


def _load_review_responses(app_id: str) -> dict[int, dict]:
    path = _DATA_DIR / app_id / "review_responses.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            data = {int(k): v for k, v in raw.items()}
            _review_responses[app_id] = data
            return data
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Human input requests  (in-memory only; needed while agent is live)
# ---------------------------------------------------------------------------

_human_input_pending: dict[str, list[dict]] = {}   # app_id → list of request dicts
_human_input_events: dict[str, asyncio.Event] = {}  # request_id → Event
_human_input_responses: dict[str, str] = {}          # request_id → response text


def init_human_input(app_id: str) -> None:
    _human_input_pending[app_id] = []


def register_human_input_request(
    app_id: str, request_id: str, question: str, context: str
) -> asyncio.Event:
    """Create a pending human-input request and return an Event to await."""
    event = asyncio.Event()
    _human_input_events[request_id] = event
    _human_input_pending.setdefault(app_id, []).append({
        "request_id": request_id,
        "question": question,
        "context": context,
    })
    return event


def submit_human_input_response(request_id: str, response: str) -> bool:
    """Deliver a human's answer, waking the waiting tool."""
    if request_id not in _human_input_events:
        return False
    _human_input_responses[request_id] = response
    _human_input_events[request_id].set()
    return True


def get_human_input_response(request_id: str) -> str | None:
    return _human_input_responses.get(request_id)


def get_pending_human_input(app_id: str) -> list[dict]:
    return _human_input_pending.get(app_id, [])
