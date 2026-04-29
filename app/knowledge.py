import pathlib
from difflib import get_close_matches

KNOWLEDGE_DIR = pathlib.Path(__file__).parent.parent / "knowledge"

_cache: dict[str, str] = {}
_alias_map: dict[str, str] = {}  # normalized alias -> canonical stem


def _normalize(s: str) -> str:
    """Lowercase, replace hyphens/spaces with underscores, strip 'd3_' prefix."""
    s = s.lower().strip().replace("-", "_").replace(" ", "_")
    for prefix in ("d3_",):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def _build_alias_map() -> None:
    """For each cached stem, register the canonical stem plus several aliases."""
    _alias_map.clear()
    for stem in _cache:
        norm = _normalize(stem)
        _alias_map[norm] = stem
        _alias_map[stem.lower()] = stem
        _alias_map[stem.lower().replace("-", "_")] = stem
        # Register each suffix: "evaluation_rubric" → also "rubric"
        parts = norm.split("_")
        for i in range(len(parts)):
            sub = "_".join(parts[i:])
            if sub and sub not in _alias_map:
                _alias_map[sub] = stem


def load_all() -> None:
    _cache.clear()
    for f in KNOWLEDGE_DIR.glob("*.md"):
        _cache[f.stem] = f.read_text()
    _build_alias_map()


def get(name: str) -> str:
    if name in _cache:
        return _cache[name]
    norm = _normalize(name)
    canonical = _alias_map.get(norm)
    if canonical:
        return _cache[canonical]
    for alias, canonical in _alias_map.items():
        if norm in alias or alias in norm:
            return _cache[canonical]
    close = get_close_matches(norm, _alias_map.keys(), n=1, cutoff=0.6)
    if close:
        return _cache[_alias_map[close[0]]]
    return ""


def list_files() -> list[str]:
    return list(_cache.keys())


def list_with_summaries() -> dict[str, str]:
    """Return {stem: one-line summary} for agent discovery.

    The summary is the first meaningful heading or non-empty line, so the
    agent knows what each file is *about* before deciding to read it.
    """
    result = {}
    for stem, content in _cache.items():
        summary = ""
        for line in content.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                summary = stripped[:120]
                break
        result[stem] = summary or "(no summary)"
    return result
