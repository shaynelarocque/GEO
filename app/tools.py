"""Tool implementations and MCP server factory for the D3 Briefing Agent.

Custom MCP tools (served via create_research_server):
  - research_fetch          Resilient web fetcher with GitHub API + Wayback Machine
  - list_knowledge_files    Discover available reference files at runtime
  - read_knowledge_file     Read a specific reference file by name
  - self_assess             Mandatory quality checkpoint after each major step
  - flag_human_review       Surface a gap or concern in the brief
  - emit_brief_section      Write a completed brief section
  - update_brief_section    Revise a previously emitted section (backtracking)
  - request_human_input     Pause and ask a human observer a question mid-run
  - save_note / read_notes  Scratch space for intermediate research findings
  - save_plan / read_plan   Persistent research plan the agent can update
  - emit_log                Narrate research progress in real time

Built-in SDK tools (configured in agent.py, not defined here):
  - WebSearch             Live web search (replaces Tavily/DuckDuckGo)
  - WebFetch              Standard page fetching + PDF reading
"""

import asyncio
import datetime as _dt
import decimal
import json
import os
import re
import uuid as uuid_mod
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse, urljoin

import httpx
import psycopg
from claude_agent_sdk import tool, create_sdk_mcp_server

from app import knowledge, store as _store


# ---------------------------------------------------------------------------
# Database access: read-only SQL against the hackathon-shared Postgres.
# ---------------------------------------------------------------------------

_KNOWN_SCHEMAS = {"cra", "fed", "ab", "general"}
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_QUERY_ROW_LIMIT = 200
_QUERY_TIMEOUT_MS = 45_000


def _strip_sql_comments(sql: str) -> str:
    return _SQL_COMMENT.sub("", sql)


def _validate_select_only(sql: str) -> str | None:
    """Return None if SQL is acceptable, else a human-readable rejection reason."""
    bare = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not bare:
        return "empty query"
    first = bare.split(None, 1)[0].lower()
    if first not in {"select", "with"}:
        return f"only SELECT or WITH … SELECT queries are allowed (got: {first!r})"
    # Reject statement chaining; a semicolon followed by more SQL.
    # We've already rstripped a trailing semicolon, so any remaining `;`
    # in the body indicates a chained statement.
    if ";" in bare:
        return "statement chaining is not allowed; submit one query per call"
    return None


def _row_to_jsonable(row: tuple, cols: list[str]) -> dict:
    out = {}
    for col, val in zip(cols, row):
        if isinstance(val, decimal.Decimal):
            out[col] = float(val) if val == val.to_integral_value() and abs(val) < 1e15 else str(val)
        elif isinstance(val, (_dt.date, _dt.datetime)):
            out[col] = val.isoformat()
        elif isinstance(val, (bytes, bytearray, memoryview)):
            out[col] = "<binary>"
        else:
            out[col] = val
    return out


async def _run_select(sql: str) -> dict:
    """Execute a validated SELECT and return {columns, rows, row_count, truncated}.

    Wrapped in run_in_executor so the synchronous psycopg call doesn't block
    the FastAPI event loop.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return {"error": "DATABASE_URL not configured"}

    def _do_query() -> dict:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {_QUERY_TIMEOUT_MS}")
                cur.execute(sql)
                if cur.description is None:
                    return {"error": "query returned no result set"}
                cols = [d.name for d in cur.description]
                # Fetch one extra row to detect truncation.
                rows = cur.fetchmany(_QUERY_ROW_LIMIT + 1)
                truncated = len(rows) > _QUERY_ROW_LIMIT
                rows = rows[:_QUERY_ROW_LIMIT]
                return {
                    "columns": cols,
                    "rows": [_row_to_jsonable(r, cols) for r in rows],
                    "row_count": len(rows),
                    "truncated": truncated,
                }

    return await asyncio.get_running_loop().run_in_executor(None, _do_query)


# ---------------------------------------------------------------------------
# Shared HTTP headers. Mimic a real browser to avoid bot blocks
# ---------------------------------------------------------------------------

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# MCP server factory. Creates per-request tools with closures over state
# ---------------------------------------------------------------------------

EmitFn = Callable[[str, str, str, str | None], Awaitable[None]]


def create_research_server(
    app_id: str,
    audit_state: dict,
    raw_data: dict,
    emit_fn: EmitFn,
    scratch_notes: dict[str, str],
    research_plan: dict,
):
    """Create a per-request MCP server with tools that close over request state.

    Args:
        app_id: The audit ID for this research session.
        audit_state: Mutable dict matching ProgramAudit shape. Tools mutate this
            in place: goal_anchor, lenses, drafts, synthesis, reasoning_trail, flags.
        raw_data: Mutable dict tracking fetched URLs, tool calls, db queries.
        emit_fn: Async callable(app_id, message, level, details=None) for SSE logging.
        scratch_notes: Mutable dict for agent's intermediate research notes.
        research_plan: Mutable dict for agent's research plan.
    """
    # Ensure the expected substructures exist; the caller may pass an empty dict.
    audit_state.setdefault("goal_anchor", None)
    audit_state.setdefault("lenses", {})
    audit_state.setdefault("drafts", [])
    audit_state.setdefault("synthesis", None)
    audit_state.setdefault("reasoning_trail", [])
    audit_state.setdefault("flags", [])

    _LENS_KEYS = {"stated_objectives", "budget", "adoption", "vendor"}
    _PHASE_KEYS = _LENS_KEYS | {"goal_anchor", "synthesis", "follow_up", "other"}
    _VERDICTS = {"green", "yellow", "red", "insufficient_evidence"}
    _TIERS = {"strong", "moderate", "limited", "n/a"}
    _INSTRUMENTS = {"atip", "order_paper_question", "committee_followup"}

    @tool(
        "research_fetch",
        "Fetch a web page with advanced strategies: GitHub API integration (richer "
        "structured data for github.com URLs), Wayback Machine fallback (if site is "
        "down or blocked), bot/login-wall detection, Next.js content extraction "
        "(both Pages Router __NEXT_DATA__ and App Router RSC payloads), "
        "Jina Reader JS-rendering fallback for SPAs, and sitemap discovery. "
        "Returns content, status, detected issues, archive data, and a structured "
        "nav_links array of {text, url} objects found on the page. Use nav_links "
        "to decide which sub-pages are worth exploring; fetch the ones relevant "
        "to your research question, skip the rest. Content truncated to 15,000 chars.",
        {"url": str},
    )
    async def research_fetch_tool(args: dict[str, Any]) -> dict[str, Any]:
        url = args["url"]
        source_hint = " [GitHub API]" if "github.com" in url else ""
        await emit_fn(app_id, f"Fetching{source_hint}: {url}", "info")
        result = await fetch_url(url)
        # Track in raw data
        raw_data["fetched_urls"][url] = {
            k: v for k, v in result.items() if k != "content"
        }
        # Emit contextual log
        issues = result.get("issues", [])
        wb = result.get("wayback_machine", {})
        content_preview = result.get("content", "")[:600]
        if "all_strategies_failed" in issues:
            await emit_fn(
                app_id,
                f"All fetch strategies failed for {url}: {result.get('error', '')}",
                "error",
                content_preview,
            )
        elif "original_unreachable" in issues and wb.get("found"):
            await emit_fn(
                app_id,
                f"Site unreachable; Wayback snapshot found ({wb['archived_at']}): {url}",
                "warning",
                content_preview,
            )
        elif wb.get("found"):
            issue_labels = ", ".join(issues) if issues else "none"
            await emit_fn(
                app_id,
                f"Fetched {url}; {result.get('content_length', 0)} chars "
                f"[issues: {issue_labels}] + Wayback archive ({wb['archived_at']})",
                "success",
                content_preview,
            )
        elif "error" in result:
            await emit_fn(app_id, f"Fetch failed for {url}: {result['error']}", "warning")
        else:
            issue_labels = f" [{', '.join(issues)}]" if issues else ""
            source = result.get("source", "")
            source_label = f" [{source}]" if source else ""
            await emit_fn(
                app_id,
                f"Fetched {url}; {result.get('content_length', 0)} chars{source_label}{issue_labels}",
                "success",
                content_preview,
            )
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "list_knowledge_files",
        "List all knowledge files available in the reference library, with a "
        "one-line summary of each. Call this at the start of research to discover "
        "what reference materials are available. Read any relevant file with "
        "read_knowledge_file.",
        {},
    )
    async def list_knowledge_files_tool(args: dict[str, Any]) -> dict[str, Any]:
        summaries = knowledge.list_with_summaries()
        lines = "\n".join(f"  - {k}: {v}" for k, v in summaries.items())
        await emit_fn(
            app_id,
            f"Knowledge library: {len(summaries)} files available",
            "info",
            lines,
        )
        result = {
            "available_files": list(summaries.keys()),
            "summaries": summaries,
            "note": "Use read_knowledge_file to read any of these.",
        }
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "read_knowledge_file",
        "Read a specific knowledge file from the reference library. Use "
        "list_knowledge_files first to see what is available. Accepts exact "
        "filename stem or common aliases (e.g. 'rubric', 'mandate', 'streams').",
        {"filename": str},
    )
    async def read_knowledge_file_tool(args: dict[str, Any]) -> dict[str, Any]:
        filename = args["filename"]
        content = knowledge.get(filename)
        if content:
            await emit_fn(
                app_id, f"Read knowledge file: {filename} ({len(content)} chars)", "info"
            )
            result = {"filename": filename, "content": content}
        else:
            available = knowledge.list_with_summaries()
            summary_lines = "\n".join(f"  - {k}: {v}" for k, v in available.items())
            await emit_fn(
                app_id,
                f"Knowledge file not found: '{filename}'; showing available files",
                "warning",
            )
            result = {
                "error": f"File '{filename}' not found.",
                "available_files": list(available.keys()),
                "summaries": summary_lines,
                "hint": "Try list_knowledge_files to see all available files with descriptions.",
            }
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    # ------------------------------------------------------------------
    # Reasoning surface: self_assess + record_pivot. Both append to
    # audit_state.reasoning_trail. The /stream endpoint surfaces these
    # to the frontend reasoning lane as 'reasoning' SSE events.
    # ------------------------------------------------------------------

    def _push_reasoning(kind: str, phase: str, headline: str, detail: str | None = None):
        item = {
            "id": str(uuid_mod.uuid4()),
            "kind": kind,
            "phase": phase,
            "headline": headline,
            "detail": detail,
        }
        audit_state["reasoning_trail"].append(item)
        return item

    @tool(
        "self_assess",
        "Record a reasoning checkpoint. Call after each major step (goal extraction, "
        "each lens, synthesis) and any time you reconsider an earlier judgment. The "
        "headline is one sentence the reviewer will read in the reasoning lane; "
        "detail is the longer reasoning. Use 'phase' to tag which audit phase this "
        "self-assessment belongs to so the lane can group items per lens.",
        {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "enum": sorted(_PHASE_KEYS),
                    "description": "Which phase this self-assessment is about.",
                },
                "headline": {
                    "type": "string",
                    "description": "One-sentence summary of what you're concluding (≤ 140 chars).",
                },
                "detail": {
                    "type": "string",
                    "description": "Longer reasoning. Cite the evidence that drove the conclusion.",
                },
            },
            "required": ["phase", "headline"],
        },
    )
    async def self_assess_tool(args: dict[str, Any]) -> dict[str, Any]:
        phase = args["phase"]
        if phase not in _PHASE_KEYS:
            return {"content": [{"type": "text", "text": json.dumps({"error": f"unknown phase {phase!r}"})}]}
        headline = args["headline"]
        detail = args.get("detail")
        _push_reasoning("self_assess", phase, headline, detail)
        await emit_fn(app_id, f"Self-assess [{phase}]: {headline}", "info", detail)
        return {"content": [{"type": "text", "text": json.dumps({"recorded": True})}]}

    @tool(
        "record_pivot",
        "Record a direction change. Call this when you abandon an instrument, "
        "back off a verdict, switch the lens you're working on, or pick a "
        "different research path. The reasoning lane surfaces pivots prominently; "
        "they are the high-value content the audience watches for. Do NOT call "
        "this for routine tool selection; only when you reconsider a meaningful "
        "earlier choice.",
        {
            "type": "object",
            "properties": {
                "from_phase": {
                    "type": "string",
                    "enum": sorted(_PHASE_KEYS),
                    "description": "The phase or lens you were working in.",
                },
                "to_phase": {
                    "type": "string",
                    "enum": sorted(_PHASE_KEYS),
                    "description": "The phase or lens you are moving to.",
                },
                "reason": {
                    "type": "string",
                    "description": "What changed your mind. One or two sentences.",
                },
            },
            "required": ["from_phase", "to_phase", "reason"],
        },
    )
    async def record_pivot_tool(args: dict[str, Any]) -> dict[str, Any]:
        from_phase = args["from_phase"]
        to_phase = args["to_phase"]
        reason = args["reason"]
        if from_phase not in _PHASE_KEYS or to_phase not in _PHASE_KEYS:
            return {"content": [{"type": "text", "text": json.dumps({"error": "unknown phase key"})}]}
        headline = f"{from_phase} → {to_phase}"
        _push_reasoning("pivot", to_phase, headline, reason)
        await emit_fn(app_id, f"Pivot: {headline}; {reason}", "warning")
        return {"content": [{"type": "text", "text": json.dumps({"recorded": True})}]}

    @tool(
        "flag_human_review",
        "Flag a lingering concern that you cannot resolve through more research. "
        "Use sparingly; for gaps that warrant an accountability instrument, call "
        "add_draft instead. Severity: critical = data integrity / blocker, "
        "high = important gap, medium = notable concern, low = minor note.",
        {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "Which lens or phase this flag belongs to"},
                "issue": {"type": "string", "description": "What the problem is"},
                "attempted": {"type": "string", "description": "What you tried before flagging"},
                "suggestion": {"type": "string", "description": "Suggested action for the human reviewer"},
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Severity level",
                },
            },
            "required": ["section", "issue", "attempted", "suggestion", "severity"],
        },
    )
    async def flag_human_review_tool(args: dict[str, Any]) -> dict[str, Any]:
        audit_state["flags"].append(args)
        severity = args.get("severity", "medium")
        await emit_fn(
            app_id,
            f"FLAG [{severity.upper()}] [{args['section']}]: {args['issue']}",
            "warning",
        )
        return {"content": [{"type": "text", "text": json.dumps({"flagged": True})}]}

    # ------------------------------------------------------------------
    # Structured audit output: goal anchor, lenses, synthesis, drafts.
    # ------------------------------------------------------------------

    @tool(
        "set_goal_anchor",
        "Write the program's accountability anchor: the program's own stated "
        "objectives, original budget, success metrics, timeline, and the sources "
        "you used. Call this once after goal extraction. Cite at least two "
        "primary_gov sources (see knowledge:provenance).",
        {
            "type": "object",
            "properties": {
                "stated_objectives": {"type": "string", "description": "1-2 paragraphs of mission language quoted from founding doc, with citation markers."},
                "original_budget": {"type": "string", "description": "Human-readable: '$X over Y years from Department of Z, originally committed YYYY-MM-DD'."},
                "success_metrics": {"type": "array", "items": {"type": "string"}, "description": "List of measurable targets the program committed to."},
                "timeline": {"type": "string", "description": "Original start, end, key milestones."},
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "source": {"type": "string"},
                            "tier": {"type": "string"},
                            "excerpt": {"type": "string"},
                        },
                        "required": ["claim", "source", "tier"],
                    },
                    "description": "Evidence list scored by provenance tier.",
                },
            },
            "required": ["stated_objectives", "sources"],
        },
    )
    async def set_goal_anchor_tool(args: dict[str, Any]) -> dict[str, Any]:
        audit_state["goal_anchor"] = {
            "stated_objectives": args.get("stated_objectives", ""),
            "original_budget": args.get("original_budget"),
            "success_metrics": args.get("success_metrics", []),
            "timeline": args.get("timeline"),
            "sources": args.get("sources", []),
        }
        await emit_fn(app_id, "Goal anchor written.", "success")
        _push_reasoning("decision", "goal_anchor", "Goal anchor recorded.", None)
        return {"content": [{"type": "text", "text": json.dumps({"saved": True})}]}

    @tool(
        "set_lens",
        "Write or revise a lens. Calling set_lens twice on the same key REVISES "
        "the lens; the previous verdict is captured as a backtrack in the "
        "reasoning trail. Required structure: verdict, evidence_tier, summary "
        "(one sentence ≤140 chars), 3-5 key_numbers, rationale_md, "
        "counter_argument_md. For the budget lens, also include budget_tranches "
        "as a time-ordered list of dollar amounts so the budget ribbon can render.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string", "enum": sorted(_LENS_KEYS), "description": "Which lens."},
                "verdict": {"type": "string", "enum": sorted(_VERDICTS), "description": "green / yellow / red / insufficient_evidence."},
                "evidence_tier": {"type": "string", "enum": sorted(_TIERS), "description": "strong / moderate / limited / n/a (when verdict is insufficient_evidence)."},
                "summary": {"type": "string", "description": "One sentence ≤140 chars; appears in the lens header card."},
                "key_numbers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                            "sublabel": {"type": "string"},
                        },
                        "required": ["label", "value"],
                    },
                    "description": "3-5 quantitative anchors. Examples: 'Original budget: $40M', 'Latest authority: $300M', 'Adoption: <5%'.",
                },
                "rationale_md": {"type": "string", "description": "Markdown body with citations: how you read the evidence."},
                "counter_argument_md": {"type": "string", "description": "Markdown: the strongest argument a defender of the program could make against your rationale."},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "source": {"type": "string"},
                            "tier": {"type": "string"},
                            "excerpt": {"type": "string"},
                        },
                        "required": ["claim", "source", "tier"],
                    },
                    "description": "Evidence list (optional but encouraged).",
                },
                "budget_tranches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "e.g. 'Founding commitment', 'Amendment 2', 'Latest authority'"},
                            "date": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the tranche."},
                            "amount_cad": {"type": "number", "description": "Dollar amount in CAD."},
                            "note": {"type": "string"},
                            "source": {"type": "string", "description": "Citation marker."},
                        },
                        "required": ["label", "amount_cad"],
                    },
                    "description": "ONLY for the budget lens. Time-ordered tranches; rendered as the ribbon timeline.",
                },
            },
            "required": ["key", "verdict", "evidence_tier", "summary", "key_numbers", "rationale_md", "counter_argument_md"],
        },
    )
    async def set_lens_tool(args: dict[str, Any]) -> dict[str, Any]:
        key = args["key"]
        if key not in _LENS_KEYS:
            return {"content": [{"type": "text", "text": json.dumps({"error": f"unknown lens key {key!r}"})}]}
        if args["verdict"] not in _VERDICTS:
            return {"content": [{"type": "text", "text": json.dumps({"error": f"unknown verdict {args['verdict']!r}"})}]}
        if args["evidence_tier"] not in _TIERS:
            return {"content": [{"type": "text", "text": json.dumps({"error": f"unknown evidence_tier {args['evidence_tier']!r}"})}]}

        previous = audit_state["lenses"].get(key)
        is_revision = previous is not None
        revision_count = (previous.get("revision_count", 0) + 1) if previous else 0

        lens = {
            "key": key,
            "verdict": args["verdict"],
            "evidence_tier": args["evidence_tier"],
            "summary": args["summary"],
            "key_numbers": args.get("key_numbers", []),
            "rationale_md": args["rationale_md"],
            "counter_argument_md": args["counter_argument_md"],
            "evidence": args.get("evidence", []),
            "budget_tranches": args.get("budget_tranches", []) if key == "budget" else [],
            "revision_count": revision_count,
        }
        audit_state["lenses"][key] = lens

        if is_revision:
            old_v = previous.get("verdict")
            new_v = args["verdict"]
            if old_v != new_v:
                _push_reasoning(
                    "backtrack",
                    key,
                    f"{key}: verdict changed {old_v} → {new_v}",
                    f"Revision #{revision_count}.",
                )
                await emit_fn(app_id, f"Lens REVISED: {key} verdict {old_v} → {new_v}", "warning")
            else:
                await emit_fn(app_id, f"Lens updated: {key} (verdict unchanged: {new_v})", "info")
        else:
            await emit_fn(app_id, f"Lens written: {key} ({args['verdict']}, {args['evidence_tier']})", "success")

        return {"content": [{"type": "text", "text": json.dumps({"saved": True, "key": key, "revision_count": revision_count})}]}

    @tool(
        "set_synthesis",
        "Write the cross-lens synthesis. Call this last, after every lens has "
        "been set. The synthesis carries an overall verdict, an overall evidence "
        "tier, a one-sentence summary, and a markdown rationale that ties the "
        "lens verdicts together.",
        {
            "type": "object",
            "properties": {
                "overall_verdict": {"type": "string", "enum": sorted(_VERDICTS)},
                "overall_tier": {"type": "string", "enum": sorted(_TIERS)},
                "summary": {"type": "string", "description": "One sentence ≤200 chars."},
                "rationale_md": {"type": "string", "description": "Markdown rationale."},
            },
            "required": ["overall_verdict", "overall_tier", "summary", "rationale_md"],
        },
    )
    async def set_synthesis_tool(args: dict[str, Any]) -> dict[str, Any]:
        if args["overall_verdict"] not in _VERDICTS or args["overall_tier"] not in _TIERS:
            return {"content": [{"type": "text", "text": json.dumps({"error": "unknown verdict or tier"})}]}
        audit_state["synthesis"] = {
            "overall_verdict": args["overall_verdict"],
            "overall_tier": args["overall_tier"],
            "summary": args["summary"],
            "rationale_md": args["rationale_md"],
        }
        await emit_fn(app_id, f"Synthesis written: {args['overall_verdict']} / {args['overall_tier']}", "success")
        _push_reasoning("decision", "synthesis", "Synthesis recorded.", args["summary"])
        return {"content": [{"type": "text", "text": json.dumps({"saved": True})}]}

    @tool(
        "add_draft",
        "Append a drafted accountability instrument. Use when a lens hits "
        "insufficient_evidence at low tier and the gap warrants an ATIP request, "
        "Order Paper question, or committee follow-up. Address by ROLE, never by "
        "name. The body should be ready for a human reviewer to edit and submit. "
        "The tool drafts; the human decides; nothing here gets sent.",
        {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": sorted(_INSTRUMENTS)},
                "addressed_to": {"type": "string", "description": "Role (e.g. 'Health Canada ATIP Coordinator'). Never a person's name."},
                "triggered_by_lens": {"type": "string", "enum": sorted(_PHASE_KEYS)},
                "triggered_by_gap": {"type": "string", "description": "Short description of the gap this instrument is meant to close."},
                "body": {"type": "string", "description": "Full draft text in markdown."},
            },
            "required": ["instrument", "addressed_to", "triggered_by_lens", "triggered_by_gap", "body"],
        },
    )
    async def add_draft_tool(args: dict[str, Any]) -> dict[str, Any]:
        draft = {
            "id": str(uuid_mod.uuid4()),
            "instrument": args["instrument"],
            "addressed_to": args["addressed_to"],
            "triggered_by_lens": args["triggered_by_lens"],
            "triggered_by_gap": args["triggered_by_gap"],
            "body": args["body"],
        }
        audit_state["drafts"].append(draft)
        await emit_fn(
            app_id,
            f"DRAFT [{args['instrument']}]: addressed to {args['addressed_to']} (gap: {args['triggered_by_gap']})",
            "success",
        )
        _push_reasoning(
            "decision",
            args["triggered_by_lens"],
            f"Drafted {args['instrument']}: {args['triggered_by_gap']}",
            args["body"][:300],
        )
        return {"content": [{"type": "text", "text": json.dumps({"saved": True, "draft_id": draft["id"]})}]}

    # ------------------------------------------------------------------
    # Improvement 2: Mid-Run Human Interaction
    # ------------------------------------------------------------------

    @tool(
        "request_human_input",
        "Pause and ask a human observer a question in real time. The question "
        "appears in the Agent Log tab and the observer can type a response. "
        "Use this when you need clarification that would materially change your "
        "research direction; e.g. 'The founder's LinkedIn shows two different "
        "companies; which is the current venture?' Use flag_human_review instead "
        "for post-hoc concerns that don't block your current work. "
        "Times out after 5 minutes if no response.",
        {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the human observer",
                },
                "context": {
                    "type": "string",
                    "description": "Background context to help the observer answer (what you found, why it matters)",
                },
            },
            "required": ["question"],
        },
    )
    async def request_human_input_tool(args: dict[str, Any]) -> dict[str, Any]:
        question = args["question"]
        context = args.get("context", "")
        request_id = str(uuid_mod.uuid4())

        await emit_fn(app_id, f"REQUESTING HUMAN INPUT: {question}", "warning", context or None)

        event = _store.register_human_input_request(app_id, request_id, question, context)

        try:
            await asyncio.wait_for(event.wait(), timeout=300.0)
            response = _store.get_human_input_response(request_id)
            await emit_fn(app_id, f"Human input received: {question}", "success", response)
            return {"content": [{"type": "text", "text": json.dumps({
                "response": response,
                "timed_out": False,
            })}]}
        except asyncio.TimeoutError:
            await emit_fn(
                app_id,
                f"Human input timed out (5 min): {question}",
                "warning",
                "No response received. Consider using flag_human_review to surface this for post-hoc review.",
            )
            return {"content": [{"type": "text", "text": json.dumps({
                "response": None,
                "timed_out": True,
                "hint": "No response received within 5 minutes. Consider using flag_human_review instead.",
            })}]}

    # ------------------------------------------------------------------
    # Improvement 3: Agent Scratch Space & Research Plan
    # ------------------------------------------------------------------

    @tool(
        "save_note",
        "Save a research note to your scratch space. Use for intermediate "
        "findings, hypotheses, or data you want to reference later. Notes "
        "persist for the duration of this research session. Overwriting an "
        "existing key updates the note.",
        {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "A short label for this note (e.g. 'founder_a_linkedin', 'competitor_list', 'open_questions')",
                },
                "content": {
                    "type": "string",
                    "description": "The note content (any format; text, markdown, structured data)",
                },
            },
            "required": ["key", "content"],
        },
    )
    async def save_note_tool(args: dict[str, Any]) -> dict[str, Any]:
        key = args["key"]
        is_update = key in scratch_notes
        scratch_notes[key] = args["content"]
        action = "updated" if is_update else "saved"
        await emit_fn(app_id, f"Note {action}: {key} ({len(args['content'])} chars)", "info")
        return {"content": [{"type": "text", "text": json.dumps({
            "saved": True, "key": key, "was_update": is_update,
            "total_notes": len(scratch_notes),
        })}]}

    @tool(
        "read_notes",
        "Read your scratch notes. Call with no key to list all note keys and "
        "previews. Call with a specific key to read that note in full.",
        {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Specific note key to read. Omit to list all notes.",
                },
            },
        },
    )
    async def read_notes_tool(args: dict[str, Any]) -> dict[str, Any]:
        key = args.get("key")
        if key:
            content = scratch_notes.get(key)
            if content:
                return {"content": [{"type": "text", "text": json.dumps({"key": key, "content": content})}]}
            return {"content": [{"type": "text", "text": json.dumps({
                "error": f"Note '{key}' not found.",
                "available_keys": list(scratch_notes.keys()),
            })}]}
        # List all notes with previews
        listing = {k: v[:200] + ("..." if len(v) > 200 else "") for k, v in scratch_notes.items()}
        return {"content": [{"type": "text", "text": json.dumps({
            "total_notes": len(scratch_notes),
            "notes": listing,
        })}]}

    @tool(
        "save_plan",
        "Save or update your research plan. Call this early to structure your "
        "approach, and again whenever your strategy changes (e.g. after discovering "
        "something unexpected). The plan is your working memory; check it with "
        "read_plan to stay on track.",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Your research plan as markdown. Include: what to research, priority order, what you've completed, what remains.",
                },
            },
            "required": ["content"],
        },
    )
    async def save_plan_tool(args: dict[str, Any]) -> dict[str, Any]:
        research_plan["revisions"] = research_plan.get("revisions", 0) + 1
        research_plan["content"] = args["content"]
        revision = research_plan["revisions"]
        label = "Research plan saved" if revision == 1 else f"Research plan updated (revision {revision})"
        await emit_fn(app_id, label, "info")
        return {"content": [{"type": "text", "text": json.dumps({
            "saved": True, "revision": revision,
        })}]}

    @tool(
        "read_plan",
        "Read your current research plan. Use this to check what you've "
        "completed and what remains before moving to the next step.",
        {},
    )
    async def read_plan_tool(args: dict[str, Any]) -> dict[str, Any]:
        content = research_plan.get("content", "")
        if not content:
            return {"content": [{"type": "text", "text": json.dumps({
                "plan": None,
                "hint": "No plan saved yet. Use save_plan to create one.",
            })}]}
        return {"content": [{"type": "text", "text": json.dumps({
            "plan": content,
            "revision": research_plan.get("revisions", 0),
        })}]}

    @tool(
        "query_db",
        "Run a read-only SQL query against the hackathon-shared Postgres. "
        "Schemas available: 'general' (cross-dataset entity resolution; usually start here), "
        "'fed' (federal grants and contributions, 1.275M rows), 'cra' (T3010 charity filings + "
        "pre-computed analyses), 'ab' (Alberta open data). Only SELECT and WITH…SELECT are "
        "allowed; statement chaining is rejected; rows are capped at 200; statement timeout "
        "is 15 seconds. Returns columns + rows + truncated flag. See knowledge:database-cookbook "
        "for ready-to-run queries. The 'schema' field is informational; your SQL should use "
        "fully-qualified table names like fed.grants_contributions or general.entity_golden_records.",
        {
            "type": "object",
            "properties": {
                "schema": {
                    "type": "string",
                    "enum": ["general", "fed", "cra", "ab"],
                    "description": "The primary schema this query targets (informational, used for logging).",
                },
                "sql": {
                    "type": "string",
                    "description": "A single SELECT or WITH…SELECT statement. Use fully-qualified table names.",
                },
                "purpose": {
                    "type": "string",
                    "description": "One sentence on what this query is meant to surface, for the audit trail.",
                },
            },
            "required": ["schema", "sql", "purpose"],
        },
    )
    async def query_db_tool(args: dict[str, Any]) -> dict[str, Any]:
        schema = args.get("schema", "")
        sql = args.get("sql", "")
        purpose = args.get("purpose", "")

        if schema not in _KNOWN_SCHEMAS:
            return {"content": [{"type": "text", "text": json.dumps({
                "error": f"unknown schema {schema!r}; expected one of {sorted(_KNOWN_SCHEMAS)}"
            })}]}

        rejection = _validate_select_only(sql)
        if rejection:
            await emit_fn(app_id, f"query_db rejected: {rejection}", "warning", sql)
            return {"content": [{"type": "text", "text": json.dumps({"error": rejection})}]}

        await emit_fn(app_id, f"query_db [{schema}]: {purpose or '(no purpose given)'}", "info", sql)

        try:
            result = await _run_select(sql)
        except psycopg.Error as e:
            await emit_fn(app_id, f"query_db error: {e}", "error", sql)
            return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}]}

        if "error" in result:
            await emit_fn(app_id, f"query_db error: {result['error']}", "error", sql)
        else:
            trunc = " (truncated)" if result.get("truncated") else ""
            await emit_fn(
                app_id,
                f"query_db returned {result['row_count']} row(s){trunc}",
                "success",
            )

        # Track in raw data for the audit trail.
        raw_data.setdefault("db_queries", []).append({
            "schema": schema,
            "purpose": purpose,
            "sql": sql,
            "row_count": result.get("row_count"),
            "truncated": result.get("truncated", False),
            "error": result.get("error"),
        })

        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    @tool(
        "emit_log",
        "Send a log message that appears in the Agent Log tab in real time. "
        "Use to narrate your research process.",
        {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The log message"},
                "level": {
                    "type": "string",
                    "enum": ["info", "warning", "error", "success"],
                    "description": "Log level for visual styling",
                },
            },
            "required": ["message"],
        },
    )
    async def emit_log_tool(args: dict[str, Any]) -> dict[str, Any]:
        await emit_fn(app_id, args["message"], args.get("level", "info"))
        return {"content": [{"type": "text", "text": json.dumps({"logged": True})}]}

    return create_sdk_mcp_server(
        name="geo-research",
        version="1.0.0",
        tools=[
            research_fetch_tool,
            query_db_tool,
            list_knowledge_files_tool,
            read_knowledge_file_tool,
            self_assess_tool,
            record_pivot_tool,
            flag_human_review_tool,
            set_goal_anchor_tool,
            set_lens_tool,
            set_synthesis_tool,
            add_draft_tool,
            request_human_input_tool,
            save_note_tool,
            read_notes_tool,
            save_plan_tool,
            read_plan_tool,
            emit_log_tool,
        ],
    )


# ---------------------------------------------------------------------------
# Helpers (used by fetch_url)
# ---------------------------------------------------------------------------

def _extract_text_from_json(obj, max_depth: int = 6, _depth: int = 0) -> str:
    """Recursively extract readable strings from a JSON structure (e.g. __NEXT_DATA__)."""
    if _depth > max_depth:
        return ""
    if isinstance(obj, str):
        stripped = obj.strip()
        if len(stripped) > 20 and not stripped.startswith(("/", "http", "{", "[")):
            return stripped
        return ""
    if isinstance(obj, list):
        parts = [_extract_text_from_json(item, max_depth, _depth + 1) for item in obj]
        return "\n".join(p for p in parts if p)
    if isinstance(obj, dict):
        # Skip internal Next.js/build keys
        skip = {
            "buildId", "__N_SSG", "__N_SSP", "isFallback", "gssp",
            "scriptLoader", "assetPrefix", "runtimeConfig", "dynamicIds",
            "appGip", "customServer",
        }
        parts = []
        for key, val in obj.items():
            if key in skip:
                continue
            extracted = _extract_text_from_json(val, max_depth, _depth + 1)
            if extracted:
                parts.append(extracted)
        return "\n".join(parts)
    return ""


def _extract_rsc_payload(html: str) -> str:
    """Extract readable text from React Server Components (RSC) flight data.

    Modern Next.js App Router embeds RSC payloads in <script> tags with
    patterns like ``self.__next_f.push([1, "..."])``.  The payloads are a
    mix of serialised React tree references and actual text content.
    """
    # Find all self.__next_f.push chunks
    chunks: list[str] = []
    for m in re.finditer(
        r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)',
        html,
        re.DOTALL,
    ):
        raw = m.group(1)
        # Un-escape the JS string
        try:
            decoded = raw.encode().decode("unicode_escape")
        except Exception:
            decoded = raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        chunks.append(decoded)

    if not chunks:
        return ""

    # RSC payload lines look like:  T1234:["$","p",null,{"children":"some text"}]
    # We extract strings that look like real prose (>20 chars, not code references)
    text_fragments: list[str] = []
    combined = "\n".join(chunks)

    # First: check if any chunk itself is plain prose (not JSON/RSC structured)
    for chunk in chunks:
        stripped = chunk.strip()
        if (
            len(stripped) > 30
            and not stripped.startswith(("{", "[", "$", "\\"))
            and "$" not in stripped
            and "className" not in stripped
        ):
            cleaned = stripped.replace("\\n", "\n").strip()
            if cleaned and cleaned not in text_fragments:
                text_fragments.append(cleaned)

    # Second: pull JSON-string-like values that are readable prose
    for fragment in re.findall(r'"([^"]{20,})"', combined):
        # Skip if it looks like code, a URL, or a React reference
        if any(c in fragment for c in ["$", "\\u", "className", "function", "import"]):
            continue
        if fragment.startswith(("http", "/", "{", "[")):
            continue
        cleaned = fragment.replace("\\n", "\n").strip()
        if cleaned and cleaned not in text_fragments:
            text_fragments.append(cleaned)

    return "\n".join(text_fragments)




async def _try_jina_reader(url: str) -> str | None:
    """Use Jina Reader (r.jina.ai) as a JS-rendering fallback.

    Jina Reader renders the page in a headless browser and returns
    clean markdown.  Free tier, no API key required.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"https://r.jina.ai/{url}",
                headers={"Accept": "text/plain"},
            )
            if resp.status_code == 200:
                text = resp.text.strip()
                if len(text) > 200:
                    if len(text) > 8000:
                        text = text[:8000] + "\n[...truncated]"
                    return text
    except Exception:
        pass
    return None


async def _try_fetch_sitemap(client: httpx.AsyncClient, page_url: str) -> list[str]:
    """Attempt to fetch sitemap.xml and return page URLs."""
    try:
        parsed = urlparse(page_url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        resp = await client.get(sitemap_url, headers=BROWSER_HEADERS, timeout=5.0)
        if resp.status_code == 200 and "<urlset" in resp.text:
            return re.findall(r"<loc>(.*?)</loc>", resp.text)
    except Exception:
        pass
    return []


async def _try_wayback_machine(client: httpx.AsyncClient, url: str) -> dict | None:
    """Check the Wayback Machine CDX API for the most recent archived snapshot.

    Returns a dict with 'content', 'url', 'archived_at' if found, else None.
    """
    try:
        cdx_url = (
            "https://web.archive.org/cdx/search/cdx"
            f"?url={url}&output=json&limit=1&fl=timestamp,original"
            "&filter=statuscode:200&collapse=digest"
        )
        cdx_resp = await client.get(cdx_url, timeout=8.0)
        if cdx_resp.status_code != 200:
            return None
        rows = cdx_resp.json()
        # rows[0] is headers ["timestamp", "original"], rows[1] is data
        if len(rows) < 2:
            return None
        timestamp, original = rows[1][0], rows[1][1]
        # Fetch the archived page (if_ modifier: returns raw content without toolbar)
        archive_url = f"https://web.archive.org/web/{timestamp}if_/{original}"
        arch_resp = await client.get(archive_url, headers=BROWSER_HEADERS, timeout=15.0)
        if arch_resp.status_code != 200:
            return None
        raw = arch_resp.text
        # Strip HTML
        text = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 8000:
            text = text[:8000] + "\n[TRUNCATED]"
        archived_date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        return {
            "content": text,
            "url": archive_url,
            "archived_at": archived_date,
        }
    except Exception:
        return None


async def _fetch_github(client: httpx.AsyncClient, url: str) -> dict:
    """Use the GitHub API to get richer data for github.com profile and repo URLs."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]

    try:
        if len(parts) == 1:
            # User profile: github.com/{username}
            username = parts[0]
            user_resp = await client.get(
                f"https://api.github.com/users/{username}",
                headers={"Accept": "application/vnd.github+json"},
                timeout=8.0,
            )
            if user_resp.status_code != 200:
                return {"url": url, "error": f"GitHub API returned {user_resp.status_code}"}

            u = user_resp.json()
            repos_resp = await client.get(
                f"https://api.github.com/users/{username}/repos?sort=updated&per_page=8",
                headers={"Accept": "application/vnd.github+json"},
                timeout=8.0,
            )
            repos = repos_resp.json() if repos_resp.status_code == 200 else []

            repo_lines = []
            for r in repos:
                if not r.get("fork"):  # skip forks
                    stars = r.get("stargazers_count", 0)
                    lang = r.get("language") or ""
                    desc = r.get("description") or ""
                    repo_lines.append(
                        f"  - {r['name']} [{lang}] ★{stars}: {desc}"
                    )

            content = f"""GitHub Profile: {u.get('name') or username} (@{username})
Bio: {u.get('bio') or '(none)'}
Location: {u.get('location') or '(not set)'}
Company: {u.get('company') or '(not set)'}
Blog/Website: {u.get('blog') or '(none)'}
Public repos: {u.get('public_repos', 0)} | Followers: {u.get('followers', 0)}

Recent / notable repositories (non-forks):
{chr(10).join(repo_lines) if repo_lines else '  (none found)'}"""

            return {
                "url": url,
                "status_code": 200,
                "content": content,
                "content_length": len(content),
                "source": "github_api",
            }

        elif len(parts) == 2:
            # Repository: github.com/{owner}/{repo}
            owner, repo_name = parts[0], parts[1]
            repo_resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo_name}",
                headers={"Accept": "application/vnd.github+json"},
                timeout=8.0,
            )
            if repo_resp.status_code != 200:
                return {"url": url, "error": f"GitHub API returned {repo_resp.status_code}"}

            r = repo_resp.json()
            # Try to get README
            readme_content = ""
            readme_resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo_name}/readme",
                headers={"Accept": "application/vnd.github.raw"},
                timeout=8.0,
            )
            if readme_resp.status_code == 200:
                readme_raw = readme_resp.text[:3000]
                readme_content = f"\n\nREADME (first 3000 chars):\n{readme_raw}"

            content = f"""GitHub Repository: {r.get('full_name')}
Description: {r.get('description') or '(none)'}
Language: {r.get('language') or '(not set)'}
Stars: {r.get('stargazers_count', 0)} | Forks: {r.get('forks_count', 0)}
Topics: {', '.join(r.get('topics', [])) or '(none)'}
Created: {r.get('created_at', '')[:10]} | Last pushed: {r.get('pushed_at', '')[:10]}
Open issues: {r.get('open_issues_count', 0)}
License: {r.get('license', {}).get('name') if r.get('license') else '(none)'}{readme_content}"""

            return {
                "url": url,
                "status_code": 200,
                "content": content,
                "content_length": len(content),
                "source": "github_api",
            }
    except Exception as e:
        return {"url": url, "error": f"GitHub API error: {e}"}

    # Fallback: just fetch the page normally
    return {}


def _detect_page_issues(content: str, status_code: int) -> list[str]:
    """Detect common page issues: bot blocks, login walls, empty shells."""
    issues = []
    lower = content.lower()

    # Bot / Cloudflare block
    if any(phrase in lower for phrase in [
        "checking your browser", "enable javascript and cookies",
        "ray id", "cloudflare", "access denied", "403 forbidden",
        "please wait while we check your browser",
    ]):
        issues.append("bot_blocked")

    # Login wall
    if any(phrase in lower for phrase in [
        "sign in to", "log in to", "please log in", "please sign in",
        "create an account to", "join to see", "you must be logged in",
        "login required", "authentication required",
    ]):
        issues.append("login_wall")

    # Essentially empty shell (JS SPA that didn't render)
    if len(content.strip()) < 200:
        issues.append("empty_shell")

    return issues


# ---------------------------------------------------------------------------
# Main fetch_url implementation
# ---------------------------------------------------------------------------

async def fetch_url(url: str) -> dict:
    """Fetch a URL with multiple fallback strategies.

    Strategy order:
    1. GitHub API (for github.com URLs; richer than HTML scraping)
    2. Normal fetch with browser User-Agent
       a. Content-type check: skip binary/PDF gracefully
       b. SPA content extraction (meta, __NEXT_DATA__, nav links)
       c. Issue detection (bot block, login wall, empty shell)
       d. Sitemap discovery if content is thin
       e. Jina Reader fallback for JS-rendered SPAs
    3. Wayback Machine fallback (if fetch failed OR site appeared dead)

    Returns nav_links as structured data so the agent can decide which
    sub-pages to explore based on its research context.
    """
    parsed = urlparse(url)
    hostname = parsed.netloc.lower().replace("www.", "")

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:

        # ── Strategy 1: GitHub API ──────────────────────────────────────────
        if hostname == "github.com":
            gh_result = await _fetch_github(client, url)
            if gh_result and "content" in gh_result:
                return gh_result
            # If API failed, fall through to normal fetch

        # ── Strategy 2: Normal fetch ────────────────────────────────────────
        fetch_error = None
        resp = None
        try:
            resp = await client.get(url, headers=BROWSER_HEADERS)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            fetch_error = str(e)
            status_code = None
            if hasattr(e, "response") and e.response is not None:
                status_code = e.response.status_code

        if resp is not None and not fetch_error:
            # Content-type check: don't try to parse binary files
            content_type = resp.headers.get("content-type", "").lower()
            if any(t in content_type for t in [
                "application/pdf", "application/octet-stream",
                "image/", "video/", "audio/",
            ]):
                ext = parsed.path.rsplit(".", 1)[-1].lower() if "." in parsed.path else "unknown"
                return {
                    "url": str(resp.url),
                    "status_code": resp.status_code,
                    "content_type": content_type,
                    "content": (
                        f"[BINARY FILE: {content_type}] This URL points to a {ext.upper()} file "
                        f"that cannot be read as text. Try using the built-in WebFetch tool "
                        f"instead; it can read PDFs natively. If that also fails, flag for "
                        f"human review so a team member can examine it manually."
                    ),
                    "content_length": 0,
                    "issues": ["binary_file"],
                }

            raw_html = resp.text
            extracted_parts = []

            # Title
            title_m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.DOTALL | re.IGNORECASE)
            if title_m:
                t = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
                if t:
                    extracted_parts.append(f"Page Title: {t}")

            # Meta tags
            for pattern, label in [
                (r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', "Meta Description"),
                (r'<meta\s+content=["\'](.*?)["\']\s+name=["\']description["\']', "Meta Description"),
                (r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', "OG Title"),
                (r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']', "OG Description"),
                (r'<meta\s+content=["\'](.*?)["\']\s+property=["\']og:description["\']', "OG Description"),
            ]:
                m = re.search(pattern, raw_html, re.IGNORECASE)
                if m and m.group(1).strip():
                    extracted_parts.append(f"{label}: {m.group(1).strip()}")

            # __NEXT_DATA__ (Next.js Pages Router)
            is_spa = False
            nd_m = re.search(
                r'<script\s+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                raw_html, re.DOTALL | re.IGNORECASE,
            )
            if nd_m:
                is_spa = True
                try:
                    nd_text = _extract_text_from_json(json.loads(nd_m.group(1)))
                    if nd_text:
                        if len(nd_text) > 5000:
                            nd_text = nd_text[:5000] + "\n[...truncated]"
                        extracted_parts.append(f"Page Content (Next.js):\n{nd_text}")
                except (json.JSONDecodeError, KeyError):
                    pass

            # RSC payload (Next.js App Router; modern apps won't have __NEXT_DATA__)
            if "self.__next_f" in raw_html:
                is_spa = True
                rsc_text = _extract_rsc_payload(raw_html)
                if rsc_text:
                    if len(rsc_text) > 5000:
                        rsc_text = rsc_text[:5000] + "\n[...truncated]"
                    extracted_parts.append(f"Page Content (Next.js App Router / RSC):\n{rsc_text}")

            # Navigation links; extract as structured data AND embed in content
            raw_nav_links = re.findall(
                r'<a\s+[^>]*href=["\'](.*?)["\'][^>]*>(.*?)</a>',
                raw_html, re.DOTALL | re.IGNORECASE,
            )
            link_texts = []
            nav_links_structured: list[dict[str, str]] = []
            seen_links: set[str] = set()
            for href, link_text in raw_nav_links[:40]:
                clean = re.sub(r"<[^>]+>", "", link_text).strip()
                if (
                    clean and href
                    and href not in seen_links
                    and not href.startswith(("#", "javascript:", "mailto:", "tel:"))
                    and len(clean) < 80
                ):
                    seen_links.add(href)
                    # Resolve relative URLs
                    full_href = urljoin(str(resp.url), href)
                    link_texts.append(f"  [{clean}]({full_href})")
                    nav_links_structured.append({"text": clean, "url": full_href})
            if link_texts:
                extracted_parts.append("Page Links:\n" + "\n".join(link_texts[:25]))

            # Standard HTML stripping for body text
            body = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.DOTALL)
            body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body).strip()

            # Combine
            combined_parts = []
            if extracted_parts:
                combined_parts.append("=== EXTRACTED METADATA ===")
                combined_parts.extend(extracted_parts)
                combined_parts.append("\n=== PAGE BODY TEXT ===")
            combined_parts.append(body)
            combined = "\n\n".join(combined_parts)

            # Issue detection
            issues = _detect_page_issues(combined, resp.status_code)

            # Sitemap if content is thin
            sitemap_urls: list[str] = []
            if len(combined) < 500:
                sitemap_urls = await _try_fetch_sitemap(client, str(resp.url))
                if sitemap_urls:
                    combined += "\n\n=== SITEMAP URLS (page content was thin) ===\n"
                    combined += "\n".join(sitemap_urls[:20])

            # Jina Reader fallback for JS-rendered SPAs with thin content
            if (len(combined) < 500 or "empty_shell" in issues) and is_spa:
                jina_content = await _try_jina_reader(url)
                if jina_content:
                    combined += f"\n\n=== JS-RENDERED CONTENT (via Jina Reader) ===\n{jina_content}"
                    if "empty_shell" in issues:
                        issues.remove("empty_shell")

            # If bot-blocked or login-walled, note it prominently
            if "bot_blocked" in issues:
                combined = (
                    "[BOT BLOCK DETECTED] This site is protected by Cloudflare or similar. "
                    "The content below may be a challenge page rather than the real page. "
                    "Consider looking for this information via alternative sources.\n\n"
                    + combined
                )
            if "login_wall" in issues:
                combined = (
                    "[LOGIN WALL DETECTED] This page requires authentication. "
                    "Only publicly visible content (if any) is shown below.\n\n"
                    + combined
                )

            if len(combined) > 15000:
                combined = combined[:15000] + "\n\n[TRUNCATED; content exceeded 15,000 characters]"

            result = {
                "url": str(resp.url),
                "status_code": resp.status_code,
                "content": combined,
                "content_length": len(combined),
                "is_spa": is_spa,
                "issues": issues,
            }
            if nav_links_structured:
                result["nav_links"] = nav_links_structured[:25]
            if sitemap_urls:
                result["sitemap_urls"] = sitemap_urls[:10]

            # ── Strategy 3: Wayback Machine (if site looks dead / blocked) ──
            site_appears_dead = (
                resp.status_code >= 500
                or "bot_blocked" in issues
                or "empty_shell" in issues
            )
            if site_appears_dead:
                wb = await _try_wayback_machine(client, url)
                if wb:
                    result["wayback_machine"] = {
                        "found": True,
                        "archived_at": wb["archived_at"],
                        "archive_url": wb["url"],
                        "content": wb["content"],
                    }
                    result["content"] += (
                        f"\n\n=== WAYBACK MACHINE SNAPSHOT ({wb['archived_at']}) ===\n"
                        f"{wb['content']}"
                    )
                else:
                    result["wayback_machine"] = {"found": False}

            return result

        # ── Strategy 3: Fetch failed entirely; try Wayback Machine ────────
        wb = await _try_wayback_machine(client, url)
        if wb:
            return {
                "url": url,
                "status_code": status_code if "status_code" in dir() else None,
                "error": fetch_error,
                "content": (
                    f"[ORIGINAL SITE UNREACHABLE: {fetch_error}]\n\n"
                    f"=== WAYBACK MACHINE SNAPSHOT ({wb['archived_at']}) ===\n"
                    f"{wb['content']}"
                ),
                "content_length": len(wb["content"]),
                "wayback_machine": {
                    "found": True,
                    "archived_at": wb["archived_at"],
                    "archive_url": wb["url"],
                },
                "issues": ["original_unreachable"],
            }

        # All strategies exhausted
        return {
            "url": url,
            "error": fetch_error or "All fetch strategies failed",
            "status_code": status_code if "status_code" in dir() else None,
            "wayback_machine": {"found": False},
            "issues": ["all_strategies_failed"],
        }
