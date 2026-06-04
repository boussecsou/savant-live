# SAVANT — Features

Every capability SAVANT offers, with real voice command examples.

---

## Real-Time Voice Conversation

SAVANT uses **Google Gemini 3.1 Flash Live** — a true bidirectional audio model, not speech-to-text + chat + TTS. This means:

- Latency is significantly lower than chained pipelines
- You can interrupt SAVANT mid-sentence (barge-in support)
- The voice is warm and conversational, not robotic
- SAVANT can reason while listening, not just after you stop speaking

**Voice:** Charon (deep, natural)

---

## VPS Command Execution

Ask SAVANT to run anything on your server. Commands execute in ~1-3 seconds inline.

```
"What's the disk usage?"
"Show me all running Docker containers"
"Tail the last 100 lines of the nginx error log"
"Check if port 443 is open"
"How much RAM is PostgreSQL using?"
"List all users on the system"
"What's in /etc/nginx/sites-enabled?"
```

SAVANT narrates the result in 1-2 sentences and displays structured output in the data panel (tables, reports, or JSON depending on the content).

---

## Complex Multi-Step Tasks (Hermes Agent)

For longer operations, SAVANT hands off to the Hermes agent — a separate LLM that can execute sequences of commands, handle errors, and verify results.

```
"Update my Notion page 'Server Status' with current metrics"
"Run a full VPS security scan and show me the report"
"Undo the last change you made"
```

While Hermes works, you see a streaming card in the data panel with each step as it happens. SAVANT speaks occasional status updates ("still working on it...").

---

## Persistent Memory

SAVANT remembers facts across sessions — no need to re-explain your setup every time.

```
"My main domain is ali-n8n.com, remember that"
"What do you know about my server?"
"Forget my old Notion workspace ID"
"Update my timezone to America/New_York"
```

Memory is stored in SQLite with namespaces:
- `infra` — server stack, containers, services
- `preferences` — language, tone, how you like to work
- `projects` — ongoing work, key dates
- `people` — team members, contacts

At the end of each session, `gemini-2.5-flash-lite` automatically extracts new durable facts from the transcript.

---

## Proactive Monitoring & Alerts

SAVANT polls your VPS metrics every 20 seconds in the background and alerts you proactively.

**Thresholds:**
- ≥ 80% — visual warning in the metrics strip (no voice interruption)
- ≥ 90% — voice interruption + alert card + incident saved to database
- Container state change — always critical

```
"Show me recent incidents"
"What alerts fired this week?"
"Resolve the RAM alert from yesterday"
```

Each critical incident is enriched by AI: SAVANT suggests the likely cause and a fix. Incidents are announced at the start of your next session if they're still unresolved.

---

## Skill Library

Skills are markdown procedure files that SAVANT can load on demand. Think of them as saved playbooks.

```
"What skills do you have?"
"Use the backup-scope skill before making changes"
"Save a new skill: how to deploy my app"
```

Skills live in `/app/skills/` (bind-mounted, persists across rebuilds). SAVANT writes new skills after solving complex problems you haven't handled before.

---

## Workflow Triggers

Register webhook URLs once, trigger them by voice.

```
"Register a workflow: name='deploy-frontend', url='https://hooks.n8n.cloud/webhook/abc123', description='Deploy frontend to production'"

"Trigger deploy-frontend"

"Trigger deploy-frontend with payload: branch=staging"

"What workflows do you know about?"
"Delete the old staging workflow"
```

Workflows bypass Hermes — they fire directly from the backend via HTTP. Response time is <30s. Auth credentials are stored encrypted and never spoken back.

---

## Console Live

An isolated voice-narrated shell accessible from the PWA data panel. Useful when you want to explore interactively while SAVANT narrates each result.

Switch to the Console tab in the PWA to access it. Type commands; SAVANT executes them and explains the output. Your working directory and user are preserved across commands.

Interactive programs (vim, top, htop, nano) are blocked — Console Live is for non-interactive shell use only.

---

## Notion Integration

SAVANT can read your Notion workspace without going through the full Hermes agent — direct REST API in ~1-3 seconds.

```
"What's in my 'Incident Log' Notion page?"
"Search my Notion for information about the PostgreSQL setup"
"Update the 'Server Status' page with today's metrics"
```

Reads are fast (direct REST). Writes go through Hermes MCP for reliability. Requires `NOTION_API_KEY` in your `.env`.

---

## Smart Session Briefing

At the start of each session, SAVANT briefs you naturally:

- What happened in recent sessions (AI summaries)
- Any unresolved incidents since your last visit
- Current VPS status (Quick Vision snapshot)

```
"What did I work on last time?"
"Any issues while I was away?"
```

---

## Hands-Free Pause / Resume

```
"Attends"  /  "Wait"  /  "Un instant"  →  SAVANT pauses, amber overlay appears
"Je suis là"  /  "I'm back"  /  "On continue"  →  resumes where it left off
```

Also auto-pauses after 180 seconds of silence.

---

## Stop & Cancel

- **Escape key** — cancels the current running action
- **Orbe button** (in PWA) — full stop: flushes audio, closes session, saves summary
- **Voice:** *"Stop"* / *"Annule"* / *"Cancel"* during a running action

---

## Control API

External control via authenticated REST endpoints (protected by `HERMES_WEBHOOK_SECRET`):

```bash
# Pause remotely (e.g., from n8n automation)
curl -X POST http://localhost:8000/control/pause \
  -H "Authorization: Bearer $HERMES_WEBHOOK_SECRET"

# Check what SAVANT is currently doing
curl http://localhost:8000/control/status \
  -H "Authorization: Bearer $HERMES_WEBHOOK_SECRET"
```
