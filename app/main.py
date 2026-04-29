import asyncio
import html as html_module
import json
import os
import re
import secrets
import uuid
from pathlib import Path

import markdown as md_lib
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app import knowledge, store
from app.agent import run_agent, run_review_update, run_general_review
from app.models import Application, Founder, LogEntry

load_dotenv()

app = FastAPI(title="D3 Briefing Agent")

# ---------------------------------------------------------------------------
# Optional HTTP Basic Auth (enabled when AUTH_USERNAME + AUTH_PASSWORD are set)
# ---------------------------------------------------------------------------

AUTH_USERNAME = os.getenv("AUTH_USERNAME")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind HTTP Basic Auth when credentials are configured."""

    async def dispatch(self, request: Request, call_next):
        # Allow health check through without auth
        if request.url.path == "/health":
            return await call_next(request)

        import base64

        auth = request.headers.get("authorization")
        if auth:
            try:
                scheme, credentials = auth.split(" ", 1)
                if scheme.lower() == "basic":
                    decoded = base64.b64decode(credentials).decode("utf-8")
                    username, password = decoded.split(":", 1)
                    if (
                        secrets.compare_digest(username, AUTH_USERNAME)
                        and secrets.compare_digest(password, AUTH_PASSWORD)
                    ):
                        return await call_next(request)
            except Exception:
                pass

        return Response(
            content="Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="BriefBot"'},
        )


if AUTH_USERNAME and AUTH_PASSWORD:
    app.add_middleware(BasicAuthMiddleware)

BASE_DIR = Path(__file__).parent.parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.on_event("startup")
async def startup():
    knowledge.load_all()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/submit")
async def submit_application(request: Request):
    form = await request.form()
    app_id = str(uuid.uuid4())

    # Parse founders from flat form fields
    founders = []
    i = 0
    while f"founder_{i}_first_name" in form:
        founder = Founder(
            first_name=form.get(f"founder_{i}_first_name", ""),
            last_name=form.get(f"founder_{i}_last_name", ""),
            email=form.get(f"founder_{i}_email", ""),
            phone=form.get(f"founder_{i}_phone", ""),
            canada_status=form.get(f"founder_{i}_canada_status", ""),
            hours_per_week=form.get(f"founder_{i}_hours_per_week", ""),
            profile_url=form.get(f"founder_{i}_profile_url", "") or None,
            current_role=form.get(f"founder_{i}_current_role", ""),
            relevant_background=form.get(f"founder_{i}_relevant_background", ""),
            prior_founding_experience=form.get(
                f"founder_{i}_prior_founding_experience", ""
            )
            or None,
        )
        founders.append(founder)
        i += 1

    # Parse SDGs (checkboxes come as repeated keys)
    sdgs = form.getlist("sdgs")

    application = Application(
        id=app_id,
        startup_name=form.get("startup_name", ""),
        problem_statement=form.get("problem_statement", ""),
        solution=form.get("solution", ""),
        sdgs=sdgs,
        prior_incubator=form.get("prior_incubator", "") or None,
        website_url=form.get("website_url", "") or None,
        canada_incorporated=form.get("canada_incorporated") == "true",
        how_team_formed=form.get("how_team_formed", ""),
        how_long_known=form.get("how_long_known", ""),
        additional_team_info=form.get("additional_team_info", "") or None,
        founders=founders,
    )

    ALLOWED_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"}
    model = form.get("model", "claude-sonnet-4-6")
    if model not in ALLOWED_MODELS:
        model = "claude-sonnet-4-6"

    store.save_application(app_id, application)
    store.init_log(app_id)

    asyncio.create_task(run_agent(app_id, application, model=model))

    return RedirectResponse(url=f"/results/{app_id}", status_code=303)


@app.get("/results/{app_id}", response_class=HTMLResponse)
async def results(request: Request, app_id: str):
    application = store.get_application(app_id)
    if not application:
        return HTMLResponse("<h1>Application not found</h1>", status_code=404)
    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "app_id": app_id,
            "application": application,
        },
    )


# ---------------------------------------------------------------------------
# SSE Stream
# ---------------------------------------------------------------------------

@app.get("/stream/{app_id}")
async def stream_log(app_id: str):
    async def event_generator():
        seen_log = 0
        seen_input = 0
        while True:
            # Log entries
            history = store.get_log_history(app_id)
            for entry in history[seen_log:]:
                html = _render_log_entry(entry)
                yield {"event": "log", "data": html}
            seen_log = len(history)

            # Human input requests (separate SSE event type)
            pending = store.get_pending_human_input(app_id)
            for req in pending[seen_input:]:
                yield {"event": "human_input", "data": json.dumps(req)}
            seen_input = len(pending)

            # Check if agent is done
            if store.get_status(app_id) in ("complete", "error"):
                yield {"event": "done", "data": ""}
                return

            await asyncio.sleep(0.2)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# API: Brief
# ---------------------------------------------------------------------------

SECTION_LABELS = {
    "synthesis": "Synthesis",
    "founder_profiles": "Founder Profiles",
    "sdg_coherence": "SDG Coherence",
    "competitive_context": "Competitive Context",
    "scorecard": "Evaluation Scorecard",
    "stream_classification": "Stream & Program Classification",
    "key_risks": "Key Risks",
    "questions_ops": "Questions for Ops (Gap-based)",
    "questions_panelists": "Questions for Panelists (Evaluation-based)",
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@app.get("/api/brief/{app_id}", response_class=HTMLResponse)
async def get_brief(app_id: str):
    brief = store.get_brief(app_id)
    if not brief:
        return HTMLResponse("<p>Brief not yet available.</p>")

    # --- Status banner ---
    status = store.get_status(app_id)
    status_html = ""
    if status == "error":
        status_html = '<div class="status-error">Agent encountered an error. Partial results shown below.</div>'

    # --- Human review flags (sorted by severity, at top) ---
    flags_html = ""
    raw_flags = brief.get("human_review_flags", [])
    unresolved_count = 0

    if raw_flags:
        sorted_flags = sorted(
            enumerate(raw_flags),
            key=lambda x: SEVERITY_ORDER.get(x[1].get("severity", "medium"), 2),
        )

        unresolved_count = sum(
            1 for idx, _ in sorted_flags
            if not (store.get_review_response(app_id, idx) or {}).get("status") == "resolved"
        )

        flags_html = '<div class="human-review-summary">'
        flags_html += f'<h3>Items Flagged for Human Review ({len(raw_flags)})</h3>'
        for idx, flag in sorted_flags:
            severity = flag.get("severity", "medium")
            review_response = store.get_review_response(app_id, idx)

            if review_response and review_response.get("status") == "resolved":
                escaped_input = html_module.escape(review_response.get("input", ""))
                flags_html += f"""<div class="flag-card flag-{severity} flag-resolved" id="flag-{idx}">
                    <div class="flag-header">
                        <span class="flag-severity-badge flag-badge-{severity}">{severity.upper()}</span>
                        <strong>{html_module.escape(flag['section'])}</strong>: Resolved
                    </div>
                    <em>Reviewer input:</em> {escaped_input}
                    <br><em>Status:</em> Section updated with new context.
                </div>"""
            else:
                flags_html += f"""<div class="flag-card flag-{severity}" id="flag-{idx}">
                    <div class="flag-header">
                        <span class="flag-severity-badge flag-badge-{severity}">{severity.upper()}</span>
                        <strong>{html_module.escape(flag['section'])}</strong>: {html_module.escape(flag['issue'])}
                    </div>
                    <em>Attempted:</em> {html_module.escape(flag['attempted'])}
                    <br><em>Suggested action:</em> {html_module.escape(flag['suggestion'])}
                </div>"""
        flags_html += "</div>"

    # --- Reviewer context box (always visible) ---
    if unresolved_count > 0:
        review_desc = f"Address any or all of the {unresolved_count} open flag(s) above, or add any other notes, corrections, or context. The AI will use your input to update the relevant sections."
        review_placeholder = "Provide clarifications, corrections, or additional context..."
    else:
        review_desc = "Add corrections, context, or observations the agent may have missed. The AI will determine which sections to update based on your input."
        review_placeholder = "e.g. 'The co-founder left D3 in January, it is now March' or 'The website is actually hosted at a different URL...'"
    review_box_html = f"""<div class="consolidated-review-form" id="reviewer-context-box">
        <div class="consolidated-review-header">Provide Additional Context</div>
        <p class="consolidated-review-desc">{review_desc}</p>
        <form hx-post="/api/review-all/{app_id}"
              hx-target="#reviewer-context-box" hx-swap="outerHTML">
            <textarea name="reviewer_input" rows="4"
                placeholder="{review_placeholder}"></textarea>
            <button type="submit" class="btn btn-primary">Submit</button>
        </form>
    </div>"""

    # --- Table of contents ---
    toc_html = '<nav class="brief-toc"><h3>Contents</h3><ul>'
    for key in SECTION_LABELS:
        if brief.get("sections", {}).get(key):
            toc_html += f'<li><a href="#section-{key}">{SECTION_LABELS[key]}</a></li>'
    toc_html += "</ul></nav>"

    # --- Brief sections ---
    sections_html = ""
    for key in SECTION_LABELS:
        content = brief.get("sections", {}).get(key, "")
        if content:
            rendered = md_lib.markdown(content, extensions=["tables", "fenced_code"])
            rendered = _transform_citations(rendered)
            sections_html += f'<section class="brief-section" id="section-{key}">'
            sections_html += f"<h2>{SECTION_LABELS[key]}</h2>"
            sections_html += rendered
            sections_html += "</section>"

    return HTMLResponse(status_html + flags_html + review_box_html + toc_html + sections_html)


# ---------------------------------------------------------------------------
# API: Human Review
# ---------------------------------------------------------------------------

@app.post("/api/review/{app_id}/{flag_index}", response_class=HTMLResponse)
async def submit_review(app_id: str, flag_index: int, reviewer_input: str = Form(...)):
    brief = store.get_brief(app_id)
    if not brief:
        return HTMLResponse("<p>Brief not found.</p>", status_code=404)

    flags = brief.get("human_review_flags", [])
    if flag_index < 0 or flag_index >= len(flags):
        return HTMLResponse("<p>Flag not found.</p>", status_code=404)

    flag = flags[flag_index]
    severity = flag.get("severity", "medium")

    # Store the response
    store.save_review_response(app_id, flag_index, {
        "input": reviewer_input,
        "status": "processing",
    })

    try:
        # Log the reviewer's submission to the Agent Log
        store.append_log_entry(app_id, LogEntry(
            message=f"REVIEWER INPUT [{flag.get('section', '')}]: {reviewer_input}",
            level="info",
            details=f"Addressing flag: {flag.get('issue', '')}",
        ))

        # Run focused mini-agent to update the relevant brief section
        updated_content = await run_review_update(
            app_id, flag, reviewer_input, brief
        )

        # Update the brief section in store
        section_key = flag.get("section", "").lower().replace(" ", "_")
        if section_key in brief.get("sections", {}):
            brief["sections"][section_key] = updated_content
            store.save_brief(app_id, brief)

        store.save_review_response(app_id, flag_index, {
            "input": reviewer_input,
            "status": "resolved",
            "updated_content": updated_content,
        })

        # Log the AI's action to the Agent Log
        store.append_log_entry(app_id, LogEntry(
            message=f"SECTION UPDATED [{flag.get('section', '')}]: Revised with reviewer context",
            level="success",
            details=f"Flag resolved. Section '{section_key}' rewritten incorporating: {reviewer_input[:200]}",
        ))

        escaped_input = html_module.escape(reviewer_input)
        return HTMLResponse(f"""<div class="flag-card flag-{severity} flag-resolved" id="flag-{flag_index}">
            <div class="flag-header">
                <span class="flag-severity-badge flag-badge-{severity}">{severity.upper()}</span>
                <strong>{html_module.escape(flag['section'])}</strong>: Resolved
            </div>
            <em>Reviewer input:</em> {escaped_input}
            <br><em>Status:</em> Section updated with new context.
        </div>""")
    except Exception as e:
        store.save_review_response(app_id, flag_index, {
            "input": reviewer_input,
            "status": "error",
            "error": str(e),
        })
        # Log the error to the Agent Log
        store.append_log_entry(app_id, LogEntry(
            message=f"REVIEW ERROR [{flag.get('section', '')}]: {e}",
            level="error",
            details=f"Reviewer input was: {reviewer_input[:200]}",
        ))
        return HTMLResponse(f"""<div class="flag-card flag-{severity}" id="flag-{flag_index}">
            <div class="flag-header">
                <span class="flag-severity-badge flag-badge-{severity}">{severity.upper()}</span>
                <strong>Error updating section:</strong> {html_module.escape(str(e))}
            </div>
            <em>Your input has been saved. The ops team can manually review.</em>
        </div>""")


# ---------------------------------------------------------------------------
# API: Consolidated Review (all flags at once)
# ---------------------------------------------------------------------------

@app.post("/api/review-all/{app_id}", response_class=HTMLResponse)
async def submit_review_all(app_id: str, reviewer_input: str = Form(...)):
    """Process reviewer clarification — against flags if any are unresolved, or as general notes."""
    brief = store.get_brief(app_id)
    if not brief:
        return HTMLResponse("<p>Brief not found.</p>", status_code=404)

    flags = brief.get("human_review_flags", [])

    # Determine if there are unresolved flags to process
    unresolved_flags = [
        (idx, flag) for idx, flag in enumerate(flags)
        if not (store.get_review_response(app_id, idx) or {}).get("status") == "resolved"
    ]

    store.append_log_entry(app_id, LogEntry(
        message=f"REVIEWER INPUT: {reviewer_input}",
        level="info",
        details=f"{'Addressing ' + str(len(unresolved_flags)) + ' unresolved flag(s)' if unresolved_flags else 'General reviewer note (no unresolved flags)'}",
    ))

    sections_updated = []

    if unresolved_flags:
        # Process each unresolved flag with the reviewer's context
        for idx, flag in unresolved_flags:
            severity = flag.get("severity", "medium")
            try:
                updated_content = await run_review_update(
                    app_id, flag, reviewer_input, brief
                )
                section_key = flag.get("section", "").lower().replace(" ", "_")
                if section_key in brief.get("sections", {}):
                    brief["sections"][section_key] = updated_content
                    store.save_brief(app_id, brief)

                store.save_review_response(app_id, idx, {
                    "input": reviewer_input,
                    "status": "resolved",
                    "updated_content": updated_content,
                })

                store.append_log_entry(app_id, LogEntry(
                    message=f"SECTION UPDATED [{flag.get('section', '')}]: Revised with reviewer context",
                    level="success",
                    details=f"Flag resolved. Section '{section_key}' rewritten.",
                ))
                sections_updated.append(section_key)

            except Exception as e:
                store.save_review_response(app_id, idx, {
                    "input": reviewer_input,
                    "status": "error",
                    "error": str(e),
                })
                store.append_log_entry(app_id, LogEntry(
                    message=f"REVIEW ERROR [{flag.get('section', '')}]: {e}",
                    level="error",
                ))
    else:
        # No unresolved flags — run general review against the whole brief
        try:
            updates = await run_general_review(app_id, reviewer_input, brief)
            for section_key, updated_content in updates.items():
                brief["sections"][section_key] = updated_content
                sections_updated.append(section_key)
            if updates:
                store.save_brief(app_id, brief)
                store.append_log_entry(app_id, LogEntry(
                    message=f"SECTIONS UPDATED: {', '.join(updates.keys())}",
                    level="success",
                    details=f"General reviewer note applied to {len(updates)} section(s).",
                ))
            else:
                store.append_log_entry(app_id, LogEntry(
                    message="Reviewer note logged — no section changes needed",
                    level="info",
                    details=reviewer_input[:200],
                ))
        except Exception as e:
            store.append_log_entry(app_id, LogEntry(
                message=f"REVIEW ERROR: {e}",
                level="error",
                details=f"Reviewer input was: {reviewer_input[:200]}",
            ))

    # Re-render just the context box (with confirmation)
    escaped_input = html_module.escape(reviewer_input)
    if sections_updated:
        section_labels = ", ".join(
            SECTION_LABELS.get(k, k) for k in sections_updated
        )
        confirmation = (
            f'<div class="flag-card flag-low flag-resolved" style="margin-bottom: 0.75rem;">'
            f'<div class="flag-header"><strong>Context applied</strong></div>'
            f'<em>Your input:</em> {escaped_input}<br>'
            f'<em>Updated sections:</em> {section_labels}'
            f'</div>'
        )
    else:
        confirmation = (
            f'<div class="flag-card flag-low flag-resolved" style="margin-bottom: 0.75rem;">'
            f'<div class="flag-header"><strong>Note recorded</strong></div>'
            f'<em>Your input:</em> {escaped_input}<br>'
            f'<em>No section changes were needed.</em>'
            f'</div>'
        )

    # Return the confirmation + a fresh form for further notes
    fresh_form = f"""<div class="consolidated-review-form" id="reviewer-context-box">
        {confirmation}
        <div class="consolidated-review-header">Add More Context</div>
        <p class="consolidated-review-desc">Continue adding corrections, context, or observations. The AI will determine which sections to update.</p>
        <form hx-post="/api/review-all/{app_id}"
              hx-target="#reviewer-context-box" hx-swap="outerHTML">
            <textarea name="reviewer_input" rows="4"
                placeholder="Additional notes, corrections, or context..."></textarea>
            <button type="submit" class="btn btn-primary">Submit</button>
        </form>
    </div>"""
    return HTMLResponse(fresh_form)


# ---------------------------------------------------------------------------
# API: Mid-Run Human Input
# ---------------------------------------------------------------------------

@app.post("/api/input/{app_id}/{request_id}", response_class=HTMLResponse)
async def submit_human_input(
    app_id: str, request_id: str, response: str = Form(...)
):
    success = store.submit_human_input_response(request_id, response)
    if not success:
        return HTMLResponse(
            "<p>Request not found or already answered.</p>", status_code=404
        )
    escaped = html_module.escape(response)
    return HTMLResponse(
        f'<div class="human-input-resolved">'
        f"<strong>Answered:</strong> {escaped}"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# API: Raw Data
# ---------------------------------------------------------------------------

@app.get("/api/raw/{app_id}", response_class=HTMLResponse)
async def get_raw_data(app_id: str):
    raw = store.get_raw_data(app_id)
    if not raw:
        return HTMLResponse("<p>Raw data not yet available.</p>")
    formatted = json.dumps(raw, indent=2, default=str)
    return HTMLResponse(f"<pre><code>{html_module.escape(formatted)}</code></pre>")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_log_entry(entry) -> str:
    ts = entry.timestamp.strftime("%H:%M:%S")
    level = entry.level.upper()
    escaped_msg = html_module.escape(entry.message)

    if entry.details:
        escaped_details = html_module.escape(entry.details)
        return (
            f'<div class="log-entry log-{entry.level} log-expandable">'
            f'<span class="log-time">{ts}</span> '
            f'<span class="log-level">[{level}]</span> '
            f'<span class="log-msg">{escaped_msg}</span>'
            f'<span class="log-expand-btn" onclick="toggleLogDetails(this)">&#x25B6;</span>'
            f'<div class="log-details" style="display:none"><pre>{escaped_details}</pre></div>'
            f"</div>"
        )
    return (
        f'<div class="log-entry log-{entry.level}">'
        f'<span class="log-time">{ts}</span> '
        f'<span class="log-level">[{level}]</span> '
        f'<span class="log-msg">{escaped_msg}</span>'
        f"</div>"
    )


def _transform_citations(html_content: str) -> str:
    """Convert [source: ...] citation patterns to clickable links or styled tags."""
    # [source: https://...] -> clickable link
    html_content = re.sub(
        r"\[source:\s*(https?://[^\]]+)\]",
        r'<a href="\1" target="_blank" rel="noopener" class="citation citation-url" title="\1">&#128279; source</a>',
        html_content,
    )

    # [source: application field: X] -> styled tag
    html_content = re.sub(
        r"\[source:\s*application field:\s*([^\]]+)\]",
        r'<span class="citation citation-field" title="Application field: \1">&#128196; \1</span>',
        html_content,
    )

    # [source: knowledge: X] -> styled tag
    html_content = re.sub(
        r"\[source:\s*knowledge:\s*([^\]]+)\]",
        r'<span class="citation citation-knowledge" title="D3 Knowledge: \1">&#128218; \1</span>',
        html_content,
    )

    return html_content
