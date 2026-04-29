import sys
import traceback

from claude_agent_sdk import (
    query,
    ClaudeSDKClient,
    ClaudeAgentOptions,
    HookMatcher,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from app import store
from app.models import Application, LogEntry
from app.prompts import build_system_prompt
from app.tools import create_research_server

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TURNS = 40


async def run_agent(app_id: str, application: Application, model: str = DEFAULT_MODEL) -> None:
    system_prompt = build_system_prompt(application)

    # Per-request mutable state (closures in create_research_server write to these)
    brief_sections: dict[str, str] = {}
    human_review_flags: list[dict] = []
    raw_data: dict = {
        "fetched_urls": {},
        "self_assessments": [],
        "tool_calls": [],
        "revision_history": [],
    }
    scratch_notes: dict[str, str] = {}
    research_plan: dict = {"content": "", "revisions": 0}

    # Initialise human-input event tracking for this run
    store.init_human_input(app_id)

    # Build custom MCP server with closures over state
    server = create_research_server(
        app_id=app_id,
        brief_sections=brief_sections,
        human_review_flags=human_review_flags,
        raw_data=raw_data,
        emit_fn=_emit,
        scratch_notes=scratch_notes,
        research_plan=research_plan,
    )

    # Hook: log every tool call to raw_data for the Raw Data tab
    async def log_tool_use(input_data, tool_use_id, context):
        raw_data["tool_calls"].append({
            "tool": input_data.get("tool_name", "unknown"),
            "input": input_data.get("tool_input", {}),
            "tool_use_id": tool_use_id,
        })
        return {}

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=["WebSearch", "WebFetch", "mcp__d3-research__*"],
        permission_mode="bypassPermissions",
        max_turns=MAX_TURNS,
        model=model,
        effort="high",
        mcp_servers={"d3-research": server},
        hooks={
            "PostToolUse": [HookMatcher(matcher=".*", hooks=[log_tool_use])],
        },
    )

    prompt = (
        "Please research this application and produce the admissions brief. "
        "Follow the Self-Assessment Protocol after each major step. "
        "Use emit_log to narrate your research process in real time."
    )

    try:
        await _emit(app_id, "Starting briefing agent...", "info")

        # ClaudeSDKClient is the correct pattern for in-process custom MCP servers
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await _emit(app_id, block.text.strip(), "info")
                elif isinstance(message, ResultMessage):
                    # ResultMessage marks the end of the agent run
                    pass

        await _emit(app_id, "Agent completed briefing.", "success")

        # Snapshot scratch space into raw_data for the audit trail
        raw_data["scratch_notes"] = scratch_notes
        raw_data["research_plan"] = research_plan

        # Save results
        store.save_brief(
            app_id,
            {
                "sections": brief_sections,
                "human_review_flags": human_review_flags,
                "metadata": {
                    "app_id": app_id,
                    "model": model,
                },
            },
        )
        store.save_raw_data(app_id, raw_data)
        store.save_log_history(app_id)
        store.set_status(app_id, "complete")

    except Exception as e:
        # Print to stderr so the error always appears in server logs
        print(f"\n[AGENT ERROR] app_id={app_id}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        # Also emit to SSE log so it shows in the Agent Log tab
        await _emit(app_id, f"Agent error: {e}", "error")
        await _emit(app_id, traceback.format_exc(), "error")
        store.set_status(app_id, "error")
        # Snapshot scratch space into raw_data
        raw_data["scratch_notes"] = scratch_notes
        raw_data["research_plan"] = research_plan
        # Save whatever we have so far
        store.save_brief(
            app_id,
            {
                "sections": brief_sections,
                "human_review_flags": human_review_flags,
                "metadata": {"error": str(e), "app_id": app_id},
            },
        )
        store.save_raw_data(app_id, raw_data)
        store.save_log_history(app_id)


async def run_review_update(
    app_id: str,
    flag: dict,
    reviewer_input: str,
    brief: dict,
) -> str:
    """Run a focused mini-agent to update a brief section based on reviewer input."""
    section_key = flag.get("section", "").lower().replace(" ", "_")
    current_content = brief.get("sections", {}).get(section_key, "")

    prompt = f"""You are updating a specific section of a D3 admissions brief based on new input from a human reviewer.

## Original Flag
- Section: {flag.get('section', '')}
- Issue: {flag.get('issue', '')}
- What was attempted: {flag.get('attempted', '')}
- Suggested action: {flag.get('suggestion', '')}

## Human Reviewer's Input
{reviewer_input}

## Current Section Content
{current_content}

## Your Task
Rewrite the section content incorporating the reviewer's new information. Keep the same markdown format and citation style. If the reviewer's input resolves the flagged issue, reflect that. If it only partially addresses the concern, note what remains unresolved.

Return ONLY the updated section content as markdown. Do not include section headers or preamble."""

    result_text = current_content

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=DEFAULT_MODEL,
            max_turns=1,
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or current_content
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    result_text = block.text.strip()

    return result_text


async def run_general_review(
    app_id: str,
    reviewer_input: str,
    brief: dict,
) -> dict[str, str]:
    """Run a mini-agent to update brief sections based on free-form reviewer notes.

    Unlike run_review_update (which targets a single flagged section), this
    handles general reviewer context that may affect multiple sections.
    Returns a dict of {section_key: updated_content} for every section changed.
    """
    sections = brief.get("sections", {})
    sections_summary = "\n".join(
        f"- **{k}**: {v[:200]}..." for k, v in sections.items() if v
    )

    prompt = f"""You are updating a D3 admissions brief based on new context from a human reviewer. The reviewer is providing additional information, corrections, or observations that the original agent may have missed.

## Human Reviewer's Input
{reviewer_input}

## Current Brief Sections
{sections_summary}

## Your Task
Determine which sections (if any) need updating based on the reviewer's input. For each section that needs changes, output the section key and the full updated content.

Format your response as one or more blocks like this:

=== SECTION: section_key ===
(full updated markdown content for that section)

If the reviewer's input doesn't materially change any section, respond with:
=== NO CHANGES NEEDED ===

Keep the same markdown format and citation style as the original. Add [source: reviewer input] citations for new facts from the reviewer."""

    # Build a second prompt with full section contents for any sections the agent identifies
    # For efficiency, include all section content
    full_sections = "\n\n".join(
        f"=== CURRENT: {k} ===\n{v}" for k, v in sections.items() if v
    )
    full_prompt = prompt + f"\n\n## Full Section Contents\n{full_sections}"

    result_text = ""

    async for message in query(
        prompt=full_prompt,
        options=ClaudeAgentOptions(
            model=DEFAULT_MODEL,
            max_turns=1,
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    result_text = block.text.strip()

    # Parse the response into section updates
    import re
    updates: dict[str, str] = {}
    if "NO CHANGES NEEDED" in result_text:
        return updates

    # Split on === SECTION: key === markers
    parts = re.split(r"===\s*SECTION:\s*(\w+)\s*===", result_text)
    # parts = ['preamble', 'section_key', 'content', 'section_key', 'content', ...]
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].strip()
        content = parts[i + 1].strip()
        if key in sections and content:
            updates[key] = content

    return updates


async def _emit(
    app_id: str, message: str, level: str = "info", details: str | None = None
) -> None:
    entry = LogEntry(message=message, level=level, details=details)
    await store.emit_log(app_id, entry)
