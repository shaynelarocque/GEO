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

from app import anthropic_catalog, knowledge, store
from app.agent import run_agent, run_review_investigation
from app.models import ProgramAuditInput, LogEntry

load_dotenv()

app = FastAPI(title="GEO: Government Engine Optimization")

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
            headers={"WWW-Authenticate": 'Basic realm="GEO"'},
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
    # Populate the Anthropic models catalog so the form dropdown is current.
    # Falls back to a hardcoded list if the API is unreachable.
    anthropic_catalog.fetch_models()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "models": anthropic_catalog.get_models(),
            "default_model_id": anthropic_catalog.get_default_id(),
            "catalog_error": anthropic_catalog.get_fetch_error(),
        },
    )


@app.get("/api/history")
async def history():
    return store.list_audits()


@app.post("/submit")
async def submit_audit(request: Request):
    form = await request.form()
    audit_id = str(uuid.uuid4())

    program_name = (form.get("program_name") or "").strip()
    if not program_name:
        return HTMLResponse("<h1>program_name is required</h1>", status_code=400)

    audit_input = ProgramAuditInput(
        id=audit_id,
        program_name=program_name,
        recipient_hint=(form.get("recipient_hint") or "").strip() or None,
    )

    submitted = (form.get("model") or "").strip()
    if submitted and anthropic_catalog.is_known_model(submitted):
        model = submitted
    else:
        model = anthropic_catalog.get_default_id() or "claude-sonnet-4-6"

    store.save_audit_input(audit_id, audit_input)
    store.init_log(audit_id)

    asyncio.create_task(run_agent(audit_id, audit_input, model=model))

    return RedirectResponse(url=f"/results/{audit_id}", status_code=303)


@app.get("/results/{app_id}", response_class=HTMLResponse)
async def results(request: Request, app_id: str):
    audit_input = store.get_audit_input(app_id)
    if not audit_input:
        return HTMLResponse("<h1>Audit not found</h1>", status_code=404)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "app_id": app_id,
            "audit_input": audit_input,
        },
    )


# ---------------------------------------------------------------------------
# SSE Stream
# ---------------------------------------------------------------------------

@app.get("/stream/{app_id}")
async def stream_log(
    app_id: str,
    since: int = 0,
    since_reasoning: int = 0,
    since_drafts: int = 0,
):
    """Stream log + reasoning + draft events via SSE. `since*` cursors let the
    frontend reconnect without re-rendering history."""
    async def event_generator():
        seen_log = max(0, since)
        seen_input = 0
        seen_reasoning = max(0, since_reasoning)
        seen_drafts = max(0, since_drafts)

        while True:
            history = store.get_log_history(app_id)
            for entry in history[seen_log:]:
                html = _render_log_entry(entry)
                yield {"event": "log", "data": html}
            seen_log = len(history)

            pending = store.get_pending_human_input(app_id)
            for req in pending[seen_input:]:
                yield {"event": "human_input", "data": json.dumps(req)}
            seen_input = len(pending)

            audit_state = store.get_audit_state(app_id) or {}
            trail = audit_state.get("reasoning_trail") or []
            for item in trail[seen_reasoning:]:
                yield {"event": "reasoning", "data": json.dumps(item, default=str)}
            seen_reasoning = len(trail)

            drafts = audit_state.get("drafts") or []
            for draft in drafts[seen_drafts:]:
                yield {"event": "draft_emitted", "data": json.dumps(draft, default=str)}
            seen_drafts = len(drafts)

            if store.get_status(app_id) in ("complete", "error"):
                yield {"event": "done", "data": json.dumps({
                    "log": seen_log,
                    "reasoning": seen_reasoning,
                    "drafts": seen_drafts,
                })}
                return

            await asyncio.sleep(0.2)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# API: Brief
# ---------------------------------------------------------------------------

LENS_LABELS = {
    "stated_objectives": "Stated Objectives",
    "budget": "Budget",
    "adoption": "Adoption",
    "vendor": "Vendor",
}

VERDICT_LABELS = {
    "green": "Green",
    "yellow": "Yellow",
    "red": "Red",
    "insufficient_evidence": "Insufficient evidence",
}

TIER_LABELS = {
    "strong": "Strong",
    "moderate": "Moderate",
    "limited": "Limited",
    "n/a": "—",
}

INSTRUMENT_LABELS = {
    "atip": "ATIP request",
    "order_paper_question": "Order Paper question",
    "committee_followup": "Committee follow-up",
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _md(content: str) -> str:
    rendered = md_lib.markdown(content or "", extensions=["tables", "fenced_code"])
    return _transform_citations(rendered)


def _format_money(amount: float) -> str:
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.0f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:,.0f}"


def _render_budget_ribbon(tranches: list[dict]) -> str:
    if not tranches:
        return ""
    nodes = []
    for i, t in enumerate(tranches):
        amount = float(t.get("amount_cad") or 0)
        label = html_module.escape(t.get("label", "") or "")
        date = html_module.escape(t.get("date") or "")
        note = html_module.escape(t.get("note") or "")
        nodes.append(
            f'<li class="ribbon-node" data-i="{i}">'
            f'<span class="ribbon-dot"></span>'
            f'<span class="ribbon-amount">{_format_money(amount)}</span>'
            f'<span class="ribbon-label">{label}</span>'
            f'<span class="ribbon-date">{date}</span>'
            f'{f"<span class=ribbon-note>{note}</span>" if note else ""}'
            f"</li>"
        )
    first_amt = float(tranches[0].get("amount_cad") or 0) or 1.0
    last_amt = float(tranches[-1].get("amount_cad") or 0)
    ratio = last_amt / first_amt if first_amt else 0
    delta = last_amt - first_amt
    if ratio >= 2:
        ratio_text = f"{ratio:.0f}× original"
    else:
        ratio_text = f"{ratio:.1f}× original"
    delta_text = f"+{_format_money(delta)} vs founding"
    return (
        '<div class="budget-ribbon">'
        '<ol class="ribbon-track">' + "".join(nodes) + "</ol>"
        f'<div class="ribbon-callout"><strong>{ratio_text}</strong><span>{delta_text}</span></div>'
        "</div>"
    )


def _render_key_numbers(items: list[dict]) -> str:
    if not items:
        return ""
    cells = []
    for kn in items[:5]:
        label = html_module.escape(kn.get("label", "") or "")
        value = html_module.escape(kn.get("value", "") or "")
        sub = html_module.escape(kn.get("sublabel", "") or "")
        cells.append(
            f'<div class="kn-cell">'
            f'<div class="kn-value">{value}</div>'
            f'<div class="kn-label">{label}</div>'
            f'{f"<div class=kn-sub>{sub}</div>" if sub else ""}'
            "</div>"
        )
    return f'<div class="key-numbers">{"".join(cells)}</div>'


def _render_lens_card(key: str, lens: dict | None, reasoning_for_phase: list[dict]) -> str:
    if not lens:
        return (
            f'<section class="lens-card lens-empty" id="lens-{key}">'
            f'<header class="lens-header">'
            f'<span class="verdict-badge verdict-pending">Pending</span>'
            f'<h2 class="lens-title">{LENS_LABELS[key]}</h2>'
            f"</header>"
            f'<div class="lens-body"><p class="empty-note">Not yet emitted.</p></div>'
            f"</section>"
        )

    verdict = lens.get("verdict", "insufficient_evidence")
    tier = lens.get("evidence_tier", "n/a")
    summary = html_module.escape(lens.get("summary", "") or "")
    rationale = lens.get("rationale_md", "") or ""
    counter = lens.get("counter_argument_md", "") or ""
    rev = lens.get("revision_count", 0)
    rev_pill = (
        f'<span class="revision-pill" title="Revised {rev} time(s)">rev {rev}</span>'
        if rev else ""
    )

    ribbon_html = ""
    if key == "budget":
        tranches = lens.get("budget_tranches", []) or []
        ribbon_html = _render_budget_ribbon(tranches)

    trail_items = ""
    if reasoning_for_phase:
        for item in reasoning_for_phase[-8:]:  # last 8 reasoning items for this lens
            kind = item.get("kind", "self_assess")
            head = html_module.escape(item.get("headline", "") or "")
            detail = html_module.escape(item.get("detail", "") or "")
            trail_items += (
                f'<li class="trail-item trail-{kind}">'
                f'<span class="trail-kind">{kind}</span>'
                f'<span class="trail-head">{head}</span>'
                f'{f"<details class=trail-detail><summary>detail</summary><div>{detail}</div></details>" if detail else ""}'
                "</li>"
            )
    trail_html = (
        f'<details class="lens-trail"><summary>Reasoning trail ({len(reasoning_for_phase)})</summary>'
        f'<ul class="trail-list">{trail_items}</ul></details>'
        if reasoning_for_phase else ""
    )

    return (
        f'<section class="lens-card verdict-{verdict.replace("_","-")}" id="lens-{key}">'
        f'<header class="lens-header">'
        f'<span class="verdict-badge verdict-{verdict.replace("_","-")}">{VERDICT_LABELS[verdict]}</span>'
        f'<h2 class="lens-title">{LENS_LABELS[key]}</h2>'
        f'<span class="tier-pill tier-{tier.replace("/","-")}">Evidence: {TIER_LABELS[tier]}</span>'
        f"{rev_pill}"
        f"</header>"
        f'<p class="lens-summary">{summary}</p>'
        f'{_render_key_numbers(lens.get("key_numbers", []))}'
        f"{ribbon_html}"
        f'<div class="lens-body">'
        f'<div class="lens-rationale"><h3>Rationale</h3>{_md(rationale)}</div>'
        f'<div class="lens-counter"><h3>Counter-argument</h3>{_md(counter)}</div>'
        f"</div>"
        f"{trail_html}"
        "</section>"
    )


def _render_draft_card(d: dict) -> str:
    instrument = d.get("instrument", "atip")
    role = html_module.escape(d.get("addressed_to", "") or "")
    gap = html_module.escape(d.get("triggered_by_gap", "") or "")
    lens = html_module.escape(d.get("triggered_by_lens", "") or "")
    body = d.get("body", "") or ""
    excerpt = body.strip().splitlines()[0] if body.strip() else ""
    excerpt = html_module.escape(excerpt[:240])
    return (
        f'<article class="draft-card draft-{instrument}" id="draft-{d.get("id","")}">'
        f'<header class="draft-header">'
        f'<span class="draft-badge draft-badge-{instrument}">{INSTRUMENT_LABELS.get(instrument, instrument)}</span>'
        f'<span class="draft-lens-tag">from {lens}</span>'
        f"</header>"
        f'<div class="draft-meta">'
        f'<div class="draft-row"><span class="draft-label">Addressed to</span><span class="draft-value">{role}</span></div>'
        f'<div class="draft-row"><span class="draft-label">Gap</span><span class="draft-value">{gap}</span></div>'
        f"</div>"
        f'<p class="draft-excerpt">{excerpt}</p>'
        f'<details class="draft-full"><summary>View full draft</summary><div class="draft-body">{_md(body)}</div></details>'
        f'<footer class="draft-chrome">Tool drafts. Human decides. The tool will not send.</footer>'
        "</article>"
    )


def _render_goal_anchor(goal: dict | None) -> str:
    if not goal:
        return (
            '<section class="goal-anchor goal-empty">'
            '<h2>Goal Anchor</h2><p class="empty-note">Not yet emitted.</p></section>'
        )
    metrics = goal.get("success_metrics") or []
    metrics_html = "".join(
        f"<li>{html_module.escape(m)}</li>" for m in metrics
    )
    return (
        '<section class="goal-anchor">'
        '<h2>Goal Anchor</h2>'
        '<div class="anchor-grid">'
        f'<div class="anchor-cell"><div class="anchor-label">Original budget</div><div class="anchor-value">{html_module.escape(goal.get("original_budget","") or "—")}</div></div>'
        f'<div class="anchor-cell"><div class="anchor-label">Timeline</div><div class="anchor-value">{html_module.escape(goal.get("timeline","") or "—")}</div></div>'
        "</div>"
        f'<div class="anchor-objectives"><div class="anchor-label">Stated objectives</div>{_md(goal.get("stated_objectives", ""))}</div>'
        f'<div class="anchor-metrics"><div class="anchor-label">Success metrics</div><ul>{metrics_html or "<li>—</li>"}</ul></div>'
        "</section>"
    )


def _render_synthesis(syn: dict | None) -> str:
    if not syn:
        return (
            '<section class="synthesis synthesis-empty">'
            '<h2>Synthesis</h2><p class="empty-note">Not yet emitted.</p></section>'
        )
    verdict = syn.get("overall_verdict", "insufficient_evidence")
    tier = syn.get("overall_tier", "n/a")
    summary = html_module.escape(syn.get("summary", "") or "")
    return (
        f'<section class="synthesis verdict-{verdict.replace("_","-")}">'
        f'<header class="lens-header">'
        f'<span class="verdict-badge verdict-{verdict.replace("_","-")}">{VERDICT_LABELS[verdict]}</span>'
        f'<h2 class="lens-title">Synthesis</h2>'
        f'<span class="tier-pill tier-{tier.replace("/","-")}">Evidence: {TIER_LABELS[tier]}</span>'
        f'</header>'
        f'<p class="lens-summary">{summary}</p>'
        f'<div class="lens-body">{_md(syn.get("rationale_md", ""))}</div>'
        '</section>'
    )


@app.get("/api/brief/{app_id}", response_class=HTMLResponse)
async def get_brief(app_id: str):
    state = store.get_audit_state(app_id)
    if not state:
        return HTMLResponse("<p>Audit not yet available.</p>")

    status = store.get_status(app_id)
    status_html = ""
    if status == "error":
        status_html = '<div class="status-error">Agent encountered an error. Partial results shown below.</div>'

    # Group reasoning items by phase for per-lens trails.
    trail = state.get("reasoning_trail") or []
    trail_by_phase: dict[str, list[dict]] = {}
    for item in trail:
        trail_by_phase.setdefault(item.get("phase", "other"), []).append(item)

    drafts = state.get("drafts") or []
    drafts_html = ""
    if drafts:
        cards = "".join(_render_draft_card(d) for d in drafts)
        drafts_html = (
            f'<section class="drafts-row" aria-label="Drafted accountability instruments">'
            f'<h2>Drafted instruments ({len(drafts)})</h2>'
            f'<div class="drafts-grid">{cards}</div>'
            '</section>'
        )

    lenses = state.get("lenses") or {}
    lens_cards = "".join(
        _render_lens_card(k, lenses.get(k), trail_by_phase.get(k, []))
        for k in ("stated_objectives", "budget", "adoption", "vendor")
    )

    review_box_html = f"""<div class="consolidated-review-form" id="reviewer-context-box">
        <div class="consolidated-review-header">Provide Additional Context</div>
        <p class="consolidated-review-desc">Paste a URL, ask a follow-up, or correct a finding. The agent investigates and updates the relevant lenses or drafts. Watch the reasoning lane on the right.</p>
        <form hx-post="/api/review-all/{app_id}"
              hx-target="#reviewer-context-box" hx-swap="outerHTML">
            <textarea name="reviewer_input" rows="4"
                placeholder="e.g. https://www.ourcommons.ca/... — or — Look at the May 6 testimony — or — Telus's IP claim is contested in the 2024 RFP."></textarea>
            <button type="submit" class="btn btn-primary">Investigate</button>
        </form>
    </div>"""

    flags = state.get("flags") or []
    flags_html = ""
    if flags:
        sorted_flags = sorted(
            enumerate(flags),
            key=lambda x: SEVERITY_ORDER.get(x[1].get("severity", "medium"), 2),
        )
        cards = []
        for idx, f in sorted_flags:
            severity = f.get("severity", "medium")
            cards.append(
                f'<div class="flag-card flag-{severity}">'
                f'<div class="flag-header"><span class="flag-severity-badge flag-badge-{severity}">{severity.upper()}</span> '
                f'<strong>{html_module.escape(f.get("section",""))}</strong>: {html_module.escape(f.get("issue",""))}</div>'
                f'<em>Attempted:</em> {html_module.escape(f.get("attempted",""))}<br>'
                f'<em>Suggested action:</em> {html_module.escape(f.get("suggestion",""))}'
                "</div>"
            )
        flags_html = (
            f'<section class="flags-section"><h2>Flags ({len(flags)})</h2>'
            + "".join(cards) + "</section>"
        )

    body = (
        status_html
        + drafts_html
        + _render_goal_anchor(state.get("goal_anchor"))
        + lens_cards
        + _render_synthesis(state.get("synthesis"))
        + flags_html
        + review_box_html
    )
    return HTMLResponse(body)


# ---------------------------------------------------------------------------
# API: Follow-up investigation (replaces per-flag review and rewrite-only paths)
# ---------------------------------------------------------------------------

@app.post("/api/review-all/{app_id}", response_class=HTMLResponse)
async def submit_review_all(app_id: str, reviewer_input: str = Form(...)):
    """Spawn a follow-up investigation agent on this audit.

    The agent gets the existing audit state plus the reviewer's input. It can
    fetch URLs, query the DB, search the web, and update sections. Streams to
    the same SSE log the initial run used.
    """
    audit_input = store.get_audit_input(app_id)
    if audit_input is None:
        return HTMLResponse("<p>Audit not found.</p>", status_code=404)

    cleaned = (reviewer_input or "").strip()
    if not cleaned:
        return HTMLResponse("<p>Empty input.</p>", status_code=400)

    store.append_log_entry(app_id, LogEntry(
        message=f"REVIEWER INPUT: {cleaned}",
        level="info",
        details="Triggering follow-up investigation.",
    ))

    asyncio.create_task(run_review_investigation(app_id, cleaned))

    escaped = html_module.escape(cleaned)
    confirmation = (
        f'<div class="flag-card flag-low flag-resolved" style="margin-bottom: 0.75rem;">'
        f'<div class="flag-header"><strong>Investigation started</strong></div>'
        f'<em>Your input:</em> {escaped}<br>'
        f'<em>Watch the Agent Log tab. The audit will update once the agent finishes.</em>'
        f'</div>'
    )

    fresh_form = f"""<div class="consolidated-review-form" id="reviewer-context-box">
        {confirmation}
        <div class="consolidated-review-header">Add More Context</div>
        <p class="consolidated-review-desc">Paste another URL, ask a follow-up, or correct a finding. Each submit kicks off a new investigation round.</p>
        <form hx-post="/api/review-all/{app_id}"
              hx-target="#reviewer-context-box" hx-swap="outerHTML">
            <textarea name="reviewer_input" rows="4"
                placeholder="Additional URL, question, or correction..."></textarea>
            <button type="submit" class="btn btn-primary">Investigate</button>
        </form>
        <script>
          if (window.geoReconnectStream) {{ window.geoReconnectStream(); }}
        </script>
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

    # [source: <schema>.<table>:<pk>=<value>] -> DB row tag
    html_content = re.sub(
        r"\[source:\s*((?:cra|fed|ab|general)\.[A-Za-z_][A-Za-z_0-9]*:[^\]]+)\]",
        r'<span class="citation citation-db" title="Database row: \1">&#128202; \1</span>',
        html_content,
    )

    # [source: knowledge: X] -> playbook tag
    html_content = re.sub(
        r"\[source:\s*knowledge:\s*([^\]]+)\]",
        r'<span class="citation citation-knowledge" title="GEO playbook: \1">&#128218; \1</span>',
        html_content,
    )

    return html_content
