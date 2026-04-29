# D3 Admissions Briefing Agent

An AI system that autonomously generates research briefs for startup applications at [District 3](https://www.district3.co), a publicly funded incubator at Concordia University in Montreal.

Built as part of my application for the [Wealthsimple AI Builder](https://jobs.ashbyhq.com/wealthsimple) role.

**[System design whiteboard (tldraw)](https://www.tldraw.com/p/nTlCrMBJp2qjNH9rjzsqr?d=v18531.1678.9725.5672.page)**

## What it does

When a new startup application is submitted, an agent autonomously produces a 9-section research brief: founder profiles, competitive analysis, SDG alignment, stream classification, scored rubric, risk flags, and interview questions — all cited to real URLs or application fields.

~15-20 minutes and ~$2.50 CAD per brief, replacing 3-4 hours of manual research.

## How it works

The agent operates in a sandbox rather than following a scripted pipeline:

- **Knowledge** — a `/knowledge` folder with D3's mandate, evaluation rubric, stream definitions, and SDG framework
- **Tools** — 13 MCP tools for web research, self-assessment, section writing, human flagging, and working memory
- **Goal** — produce 9 brief sections, all cited, with a recommendation
- **Guidelines** — citation requirements, confidence thresholds, a mandate to revise earlier work when later findings contradict it

Within this sandbox, the agent decides what to research, in what order, and how deep to go.

## Key design decisions

- **Humans decide, AI prepares.** The agent has no tool to accept, reject, email, or route. The responsibility boundary is architectural.
- **Self-correcting.** The agent self-assesses after each research phase and revises earlier sections when new evidence contradicts them.
- **Human-in-the-loop.** Two touchpoints: mid-run questions (with 5-min timeout) and post-brief review where a mini-agent rewrites sections based on reviewer input.
- **Research resilience.** Fetch cascade: GitHub API, direct HTTP, agent-driven sub-page exploration, Jina Reader (JS rendering), Wayback Machine. If all fail, the agent flags the gap honestly.

## Setup

```bash
# Clone and install
git clone https://github.com/shaynelarocque/briefbot.git
cd briefbot
pip install -r requirements.txt

# Add your API key
cp .env.example .env
# Edit .env with your Anthropic API key

# Run
uvicorn app.main:app --reload
```

Open `http://localhost:8000` to submit an application and watch the agent work.

## Deploy to Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/shaynelarocque/briefbot)

Or manually:

1. Create a new **Web Service** on [Render](https://render.com) and connect this repo
2. Set environment variables:
   - `ANTHROPIC_API_KEY` — your Anthropic API key
   - `AUTH_USERNAME` — reviewer username (optional, enables Basic Auth)
   - `AUTH_PASSWORD` — reviewer password (optional, enables Basic Auth)
3. Render will auto-detect the Dockerfile and deploy

When `AUTH_USERNAME` and `AUTH_PASSWORD` are both set, every page is gated behind HTTP Basic Auth — share the credentials with reviewers for private access.

## Stack

- **Agent:** [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents-and-tools/claude-agent-sdk) (Claude Sonnet 4.6)
- **Backend:** FastAPI, SSE streaming
- **Frontend:** Vanilla JS, HTMX
- **Storage:** File-based (no database)
