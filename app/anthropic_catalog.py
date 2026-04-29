"""Anthropic models catalog. Fetched live so the model dropdown stays current.

Loads on first call (e.g. during FastAPI startup) and caches in-process.
Falls back to a hardcoded list if the API is unreachable so the form still renders.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

_API_URL = "https://api.anthropic.com/v1/models"
_API_VERSION = "2023-06-01"

# Used only when the live fetch fails. Keep small and current-ish.
_FALLBACK = [
    {"id": "claude-opus-4-6", "display_name": "Claude Opus 4.6"},
    {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
    {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
]

# Sensible default selection if present in the catalog. First match wins.
_DEFAULT_PREFERENCE = (
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
)


@dataclass(frozen=True)
class Model:
    id: str
    display_name: str


_cache: list[Model] = []
_default_id: str = ""
_fetch_error: str = ""


def _sort_key(m: dict) -> tuple:
    """Newest first by created_at when available, otherwise by id descending."""
    return (m.get("created_at") or "", m.get("id") or "")


def fetch_models() -> list[Model]:
    """Hit /v1/models, populate the cache, set the default. Idempotent."""
    global _cache, _default_id, _fetch_error
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        _fetch_error = "ANTHROPIC_API_KEY not set"
        _cache = [Model(**m) for m in _FALLBACK]
        _default_id = _pick_default(_cache)
        return _cache

    try:
        resp = httpx.get(
            _API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _API_VERSION,
            },
            params={"limit": 100},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        # Newest first.
        data.sort(key=_sort_key, reverse=True)
        _cache = [
            Model(id=m["id"], display_name=m.get("display_name") or m["id"])
            for m in data
            if m.get("id")
        ]
        _fetch_error = ""
    except Exception as e:
        _fetch_error = f"{type(e).__name__}: {e}"
        _cache = [Model(**m) for m in _FALLBACK]

    _default_id = _pick_default(_cache)
    return _cache


def _pick_default(models: list[Model]) -> str:
    ids = {m.id for m in models}
    for preferred in _DEFAULT_PREFERENCE:
        if preferred in ids:
            return preferred
    return models[0].id if models else ""


def get_models() -> list[Model]:
    if not _cache:
        fetch_models()
    return _cache


def get_default_id() -> str:
    if not _cache:
        fetch_models()
    return _default_id


def get_fetch_error() -> str:
    return _fetch_error


def is_known_model(model_id: str) -> bool:
    return any(m.id == model_id for m in get_models())
