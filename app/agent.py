import sys
import traceback

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    HookMatcher,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from app import store
from app.models import ProgramAuditInput, LogEntry
from app.prompts import build_system_prompt, build_investigation_prompt
from app.tools import create_research_server

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TURNS = 40
INVESTIGATION_MAX_TURNS = 15


async def run_agent(app_id: str, audit_input: ProgramAuditInput, model: str = DEFAULT_MODEL) -> None:
    system_prompt = build_system_prompt(audit_input)

    audit_state: dict = store.empty_audit_state()
    audit_state["metadata"] = {
        "app_id": app_id,
        "program_name": audit_input.program_name,
        "recipient_hint": audit_input.recipient_hint,
        "model": model,
    }

    raw_data: dict = {
        "fetched_urls": {},
        "tool_calls": [],
        "audit_input": {
            "program_name": audit_input.program_name,
            "recipient_hint": audit_input.recipient_hint,
        },
    }
    scratch_notes: dict[str, str] = {}
    research_plan: dict = {"content": "", "revisions": 0}

    store.init_human_input(app_id)
    # Make the live dict visible to /stream so reasoning + draft events
    # surface in real time, not just after the run finishes.
    store.register_live_audit_state(app_id, audit_state)

    server = create_research_server(
        app_id=app_id,
        audit_state=audit_state,
        raw_data=raw_data,
        emit_fn=_emit,
        scratch_notes=scratch_notes,
        research_plan=research_plan,
    )

    async def log_tool_use(input_data, tool_use_id, context):
        raw_data["tool_calls"].append({
            "tool": input_data.get("tool_name", "unknown"),
            "input": input_data.get("tool_input", {}),
            "tool_use_id": tool_use_id,
        })
        return {}

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=["WebSearch", "WebFetch", "mcp__geo-research__*"],
        permission_mode="bypassPermissions",
        max_turns=MAX_TURNS,
        model=model,
        effort="high",
        mcp_servers={"geo-research": server},
        hooks={
            "PostToolUse": [HookMatcher(matcher=".*", hooks=[log_tool_use])],
        },
    )

    prompt = (
        f"Audit the federal program: {audit_input.program_name}. "
        f"{('Recipient hint: ' + audit_input.recipient_hint + '. ') if audit_input.recipient_hint else ''}"
        "Read your playbook, plan the audit, resolve the canonical entity, extract the goal anchor, "
        "then run the four lenses in order using set_lens. Self-assess each step via self_assess. "
        "Record direction changes via record_pivot. Synthesize via set_synthesis. Add drafts via add_draft."
    )

    try:
        await _emit(app_id, f"Starting GEO audit for: {audit_input.program_name}", "info")

        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await _emit(app_id, block.text.strip(), "info")
                elif isinstance(message, ResultMessage):
                    pass

        await _emit(app_id, "Audit complete.", "success")

        raw_data["scratch_notes"] = scratch_notes
        raw_data["research_plan"] = research_plan

        store.save_audit_state(app_id, audit_state)
        store.save_raw_data(app_id, raw_data)
        store.save_log_history(app_id)
        store.set_status(app_id, "complete")

    except Exception as e:
        print(f"\n[AGENT ERROR] app_id={app_id}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        await _emit(app_id, f"Agent error: {e}", "error")
        await _emit(app_id, traceback.format_exc(), "error")
        store.set_status(app_id, "error")
        raw_data["scratch_notes"] = scratch_notes
        raw_data["research_plan"] = research_plan
        audit_state["metadata"]["error"] = str(e)
        store.save_audit_state(app_id, audit_state)
        store.save_raw_data(app_id, raw_data)
        store.save_log_history(app_id)


async def run_review_investigation(
    app_id: str,
    reviewer_input: str,
    model: str = DEFAULT_MODEL,
) -> None:
    """Follow-up investigation triggered by reviewer input.

    Loads the existing audit state from disk, hands it back to a fresh agent
    with the full sandbox of tools, runs a focused investigation against the
    reviewer's input, and re-saves the updated audit. Streams to the same
    SSE log stream the initial run used.
    """
    audit_input = store.get_audit_input(app_id)
    if audit_input is None:
        await _emit(app_id, "Cannot investigate: audit input missing from store.", "error")
        return

    audit_state = store.get_audit_state(app_id) or store.empty_audit_state()
    # Make sure all expected keys exist (for older audits or fresh state).
    for k, v in store.empty_audit_state().items():
        audit_state.setdefault(k, v)

    raw_data = store.get_raw_data(app_id) or {
        "fetched_urls": {},
        "tool_calls": [],
    }
    scratch_notes: dict[str, str] = dict(raw_data.get("scratch_notes", {}) or {})
    research_plan: dict = dict(raw_data.get("research_plan", {"content": "", "revisions": 0}) or {"content": "", "revisions": 0})

    store.init_human_input(app_id)
    store.set_status(app_id, "processing")
    store.register_live_audit_state(app_id, audit_state)

    server = create_research_server(
        app_id=app_id,
        audit_state=audit_state,
        raw_data=raw_data,
        emit_fn=_emit,
        scratch_notes=scratch_notes,
        research_plan=research_plan,
    )

    raw_data.setdefault("review_rounds", []).append({
        "input": reviewer_input,
        "model": model,
    })

    async def log_tool_use(input_data, tool_use_id, context):
        raw_data["tool_calls"].append({
            "tool": input_data.get("tool_name", "unknown"),
            "input": input_data.get("tool_input", {}),
            "tool_use_id": tool_use_id,
            "round": "review",
        })
        return {}

    options = ClaudeAgentOptions(
        system_prompt=build_investigation_prompt(audit_input, audit_state, reviewer_input),
        allowed_tools=["WebSearch", "WebFetch", "mcp__geo-research__*"],
        permission_mode="bypassPermissions",
        max_turns=INVESTIGATION_MAX_TURNS,
        model=model,
        effort="high",
        mcp_servers={"geo-research": server},
        hooks={
            "PostToolUse": [HookMatcher(matcher=".*", hooks=[log_tool_use])],
        },
    )

    prompt = (
        f"Reviewer input: {reviewer_input}\n\n"
        "Investigate per the system prompt. Use set_lens to revise lenses, add_draft for new instruments, "
        "self_assess + record_pivot to narrate the reasoning."
    )

    try:
        await _emit(app_id, "Follow-up investigation started.", "info")

        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await _emit(app_id, block.text.strip(), "info")
                elif isinstance(message, ResultMessage):
                    pass

        await _emit(app_id, "Investigation complete.", "success")

        raw_data["scratch_notes"] = scratch_notes
        raw_data["research_plan"] = research_plan
        audit_state.setdefault("metadata", {})["review_rounds"] = len(raw_data.get("review_rounds", []))

        store.save_audit_state(app_id, audit_state)
        store.save_raw_data(app_id, raw_data)
        store.save_log_history(app_id)
        store.set_status(app_id, "complete")

    except Exception as e:
        print(f"\n[INVESTIGATION ERROR] app_id={app_id}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        await _emit(app_id, f"Investigation error: {e}", "error")
        await _emit(app_id, traceback.format_exc(), "error")
        store.set_status(app_id, "error")
        raw_data["scratch_notes"] = scratch_notes
        raw_data["research_plan"] = research_plan
        audit_state.setdefault("metadata", {})["investigation_error"] = str(e)
        store.save_audit_state(app_id, audit_state)
        store.save_raw_data(app_id, raw_data)
        store.save_log_history(app_id)


async def _emit(
    app_id: str, message: str, level: str = "info", details: str | None = None
) -> None:
    entry = LogEntry(message=message, level=level, details=details)
    await store.emit_log(app_id, entry)
