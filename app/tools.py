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
import json
import re
import uuid as uuid_mod
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse, urljoin

import httpx
from claude_agent_sdk import tool, create_sdk_mcp_server

from app import knowledge, store as _store


# ---------------------------------------------------------------------------
# Shared HTTP headers — mimic a real browser to avoid bot blocks
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
# MCP server factory — creates per-request tools with closures over state
# ---------------------------------------------------------------------------

EmitFn = Callable[[str, str, str, str | None], Awaitable[None]]


def create_research_server(
    app_id: str,
    brief_sections: dict[str, str],
    human_review_flags: list[dict],
    raw_data: dict,
    emit_fn: EmitFn,
    scratch_notes: dict[str, str],
    research_plan: dict,
):
    """Create a per-request MCP server with tools that close over request state.

    Args:
        app_id: The application ID for this research session.
        brief_sections: Mutable dict where brief sections are stored as they're written.
        human_review_flags: Mutable list where flags are appended.
        raw_data: Mutable dict tracking fetched URLs, self-assessments, and tool calls.
        emit_fn: Async callable(app_id, message, level, details=None) for SSE logging.
        scratch_notes: Mutable dict for agent's intermediate research notes.
        research_plan: Mutable dict for agent's research plan.
    """

    @tool(
        "research_fetch",
        "Fetch a web page with advanced strategies: GitHub API integration (richer "
        "structured data for github.com URLs), Wayback Machine fallback (if site is "
        "down or blocked), bot/login-wall detection, Next.js content extraction "
        "(both Pages Router __NEXT_DATA__ and App Router RSC payloads), "
        "Jina Reader JS-rendering fallback for SPAs, and sitemap discovery. "
        "Returns content, status, detected issues, archive data, and a structured "
        "nav_links array of {text, url} objects found on the page. Use nav_links "
        "to decide which sub-pages are worth exploring — fetch the ones relevant "
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
                f"Site unreachable — Wayback snapshot found ({wb['archived_at']}): {url}",
                "warning",
                content_preview,
            )
        elif wb.get("found"):
            issue_labels = ", ".join(issues) if issues else "none"
            await emit_fn(
                app_id,
                f"Fetched {url} — {result.get('content_length', 0)} chars "
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
                f"Fetched {url} — {result.get('content_length', 0)} chars{source_label}{issue_labels}",
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
                f"Knowledge file not found: '{filename}' — showing available files",
                "warning",
            )
            result = {
                "error": f"File '{filename}' not found.",
                "available_files": list(available.keys()),
                "summaries": summary_lines,
                "hint": "Try list_knowledge_files to see all available files with descriptions.",
            }
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "self_assess",
        "Self-assess the quality of your most recent research step. Call this "
        "after each major step (website analysis, each founder profile, scoring). "
        "Provide a confidence score and reasoning. If confidence < 0.6, retry or "
        "flag for human review. MANDATORY after every major step.",
        {
            "type": "object",
            "properties": {
                "step_name": {"type": "string", "description": "Name of the step just completed"},
                "confidence": {"type": "number", "description": "Confidence score from 0.0 to 1.0"},
                "reasoning": {"type": "string", "description": "Why this confidence level?"},
                "action": {
                    "type": "string",
                    "enum": ["proceed", "retry", "flag_human_review"],
                    "description": "What to do next based on this assessment",
                },
            },
            "required": ["step_name", "confidence", "reasoning", "action"],
        },
    )
    async def self_assess_tool(args: dict[str, Any]) -> dict[str, Any]:
        raw_data["self_assessments"].append(args)
        confidence = args["confidence"]
        step = args["step_name"]
        action = args["action"]
        level = "success" if confidence >= 0.6 else "warning"
        await emit_fn(
            app_id,
            f"Self-assessment [{step}]: confidence={confidence:.2f}, action={action}",
            level,
            args["reasoning"],
        )
        result = {"acknowledged": True, "action": action}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "flag_human_review",
        "Flag an item for human review. Use when data is unavailable, ambiguous, "
        "or you cannot make a confident assessment after exhausting research. "
        "Creates a visible flag in the brief. "
        "Severity: critical = data integrity/blocker, high = important gap, "
        "medium = notable concern, low = minor note.",
        {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "Which brief section this flag belongs to"},
                "issue": {"type": "string", "description": "What the problem is"},
                "attempted": {"type": "string", "description": "What you tried before flagging (list every URL attempted)"},
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
        human_review_flags.append(args)
        severity = args.get("severity", "medium")
        await emit_fn(
            app_id,
            f"HUMAN REVIEW [{severity.upper()}] [{args['section']}]: {args['issue']}",
            "warning",
        )
        return {"content": [{"type": "text", "text": json.dumps({"flagged": True})}]}

    @tool(
        "emit_brief_section",
        "Emit a completed section of the brief. Call as you complete each "
        "section so progress is visible in real time.",
        {
            "type": "object",
            "properties": {
                "section_key": {
                    "type": "string",
                    "enum": [
                        "synthesis",
                        "founder_profiles",
                        "sdg_coherence",
                        "competitive_context",
                        "scorecard",
                        "stream_classification",
                        "key_risks",
                        "questions_ops",
                        "questions_panelists",
                    ],
                    "description": "The section identifier",
                },
                "content": {
                    "type": "string",
                    "description": "The section content as markdown with citations",
                },
            },
            "required": ["section_key", "content"],
        },
    )
    async def emit_brief_section_tool(args: dict[str, Any]) -> dict[str, Any]:
        key = args["section_key"]
        brief_sections[key] = args["content"]
        await emit_fn(app_id, f"Brief section completed: {key}", "success")
        result = {"saved": True, "section_key": key}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    # ------------------------------------------------------------------
    # Improvement 1: Section Revision & Backtracking
    # ------------------------------------------------------------------

    @tool(
        "update_brief_section",
        "Revise a previously emitted brief section. Use this when later research "
        "contradicts, enriches, or materially changes an earlier section. Requires "
        "a reason so the revision is auditable. The revision is logged and the "
        "old content is preserved in the audit trail.",
        {
            "type": "object",
            "properties": {
                "section_key": {
                    "type": "string",
                    "enum": [
                        "synthesis",
                        "founder_profiles",
                        "sdg_coherence",
                        "competitive_context",
                        "scorecard",
                        "stream_classification",
                        "key_risks",
                        "questions_ops",
                        "questions_panelists",
                    ],
                    "description": "The section to revise",
                },
                "content": {
                    "type": "string",
                    "description": "The updated section content as markdown with citations",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this section is being revised (e.g. 'Found contradicting data about co-founder background')",
                },
            },
            "required": ["section_key", "content", "reason"],
        },
    )
    async def update_brief_section_tool(args: dict[str, Any]) -> dict[str, Any]:
        key = args["section_key"]
        reason = args["reason"]
        old_content = brief_sections.get(key)

        if old_content is None:
            # No previous version — treat as first write
            brief_sections[key] = args["content"]
            await emit_fn(app_id, f"Brief section completed: {key} (no prior version to revise)", "success")
            return {"content": [{"type": "text", "text": json.dumps({"saved": True, "section_key": key, "was_revision": False})}]}

        # Track the revision in raw_data
        raw_data.setdefault("revision_history", []).append({
            "section_key": key,
            "reason": reason,
            "old_content_length": len(old_content),
            "new_content_length": len(args["content"]),
        })

        brief_sections[key] = args["content"]
        await emit_fn(
            app_id,
            f"Brief section REVISED: {key} — {reason}",
            "warning",
            f"Previous version was {len(old_content)} chars, new version is {len(args['content'])} chars.",
        )
        return {"content": [{"type": "text", "text": json.dumps({"saved": True, "section_key": key, "was_revision": True, "reason": reason})}]}

    # ------------------------------------------------------------------
    # Improvement 2: Mid-Run Human Interaction
    # ------------------------------------------------------------------

    @tool(
        "request_human_input",
        "Pause and ask a human observer a question in real time. The question "
        "appears in the Agent Log tab and the observer can type a response. "
        "Use this when you need clarification that would materially change your "
        "research direction — e.g. 'The founder's LinkedIn shows two different "
        "companies — which is the current venture?' Use flag_human_review instead "
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
                    "description": "The note content (any format — text, markdown, structured data)",
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
        "something unexpected). The plan is your working memory — check it with "
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
        name="d3-research",
        version="1.0.0",
        tools=[
            research_fetch_tool,
            list_knowledge_files_tool,
            read_knowledge_file_tool,
            self_assess_tool,
            flag_human_review_tool,
            emit_brief_section_tool,
            update_brief_section_tool,
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
    1. GitHub API (for github.com URLs — richer than HTML scraping)
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
                        f"instead — it can read PDFs natively. If that also fails, flag for "
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

            # RSC payload (Next.js App Router — modern apps won't have __NEXT_DATA__)
            if "self.__next_f" in raw_html:
                is_spa = True
                rsc_text = _extract_rsc_payload(raw_html)
                if rsc_text:
                    if len(rsc_text) > 5000:
                        rsc_text = rsc_text[:5000] + "\n[...truncated]"
                    extracted_parts.append(f"Page Content (Next.js App Router / RSC):\n{rsc_text}")

            # Navigation links — extract as structured data AND embed in content
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
                combined = combined[:15000] + "\n\n[TRUNCATED — content exceeded 15,000 characters]"

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

        # ── Strategy 3: Fetch failed entirely — try Wayback Machine ────────
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
