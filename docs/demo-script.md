# Airen — 3-minute demo video script

**Target length:** 2:45 – 3:00
**Format:** screencast with voiceover (no presenter face)
**Tools:** OBS / QuickTime screen recording · Mic for narration

---

## Pre-flight checklist (do these BEFORE you hit record)

```bash
# 1. Phoenix running
phoenix serve &   # listens on localhost:6006

# 2. Fresh synthetic data so Sentinel has signal
conda activate mlre
python -m demo.synthetic_data
# → 200 spans, 79% healthy / 20% bugged / 1% failed

# 3. Web UI running
python -m airen.run_web &   # localhost:8000

# 4. .env confirmed
# AIREN_LLM_MODE=mock   (use mock for the demo — instant, free, deterministic)
# OR AIREN_LLM_BACKEND=azure with the right keys (real LLM, ~3 min)
# OR AIREN_LLM_BACKEND=gemini with paid quota

# 5. Slack channel is the one shown in the demo
# Open Slack to that channel in a second window
```

**Browser tabs (pre-loaded, left-to-right):**
1. `http://localhost:8000/` — Airen home (empty fleet view will become populated)
2. `http://localhost:8000/live` — LIVE feed (will redirect after click)
3. Slack — the incident channel
4. `http://localhost:6006` — Phoenix (for the "watch the agent observe itself" moment)

---

## Storyboard

### 🎬 SCENE 1 — The hook (0:00 – 0:15)

**Visual:** Title card. Black background. White text appears one phrase at a time.

```
              When your ML model breaks at 2am…
                       ↓
              you find out in 5 minutes.
                       ↓
                Not 5 days. Not 5 dashboards.

                        AIREN
                Autonomous ML Reliability Engineer

           Built with Gemini + ADK + Arize Phoenix MCP
```

**Voiceover (15 sec):**
> "Production ML models drift. Sometimes the regression is a model change, sometimes it's a config flag in inference code that training never saw. Either way, the engineer-on-call usually finds out hours or days too late.
> Airen is a multi-agent system that does the on-call diagnosis itself."

---

### 🎬 SCENE 2 — The real war story (0:15 – 0:35)

**Visual:** A simple diagram fades in:

```
   Feb 19, 2026 — Fritolay LSTM in production

   Inference code added:  api_fetch_limit = 73
   Training code used:    ~184 pings (no limit)

         Result: MAE for long-haul predictions
                 doubled from 610 → 1,277 minutes
                 Undetected for days.
```

**Voiceover (20 sec):**
> "Here's the story Airen was built around — a real incident from earlier this year. A commit added a parameter the model was never trained against. Long-haul predictions doubled in error. Nobody noticed for days. Watch what Airen does with that exact scenario."

---

### 🎬 SCENE 3 — The dashboard (0:35 – 0:50)

**Action:** Click to browser tab 1 — `http://localhost:8000/`

**Visual:** Show the empty dashboard, then hover over the **▶ Run new check on tl-eta** button.

**Voiceover (15 sec):**
> "This is Airen's dashboard. The tl-eta-prediction service is configured. I'll hit 'Run new check' — this kicks off the full agent pipeline."

**Action:** Click the **▶ Run new check** button.

---

### 🎬 SCENE 4 — The agents in action (0:50 – 2:00)

**Visual:** Browser redirects to `/live/<run_id>`. Live agent feed starts streaming events one by one, with a red pulsing dot in the corner.

Events that appear (each animates in):

```
🛡  MONITORING       sentinel   Checking 'tl-eta-prediction' health …
⚠️  ANOMALY_DETECTED sentinel   Sentinel: CRITICAL — mae @ api_fetch_limit=73 ratio 4.21x
🔎 INVESTIGATING    investigator Investigating against repo cloudqwest/dynamic_eta_prediction…
⚠️  ANOMALY_DETECTED investigator confidence 0.75 — commit 6270ca55 by akashlfk
📝 RCA_DRAFTING     rca_writer  Drafting incident report for humans…
📝 RCA_DRAFTING     rca_writer  Report drafted: 'TL ETA: prediction error 4.2× baseline …' (CRITICAL)
⏸ AWAITING_APPROVAL remediation Plan: REVERT_COMMIT commit 6270ca55. Branch: airen/revert-…
📣 NOTIFYING        slack       Posting incident to Slack…
📣 NOTIFYING        slack       Posted: https://airenglobal.slack.com/archives/…
✅ VALIDATING       validator   Phase 1 verdict: 🟢 PASS — all flagged segments healthy
✅ VALIDATING       validator   Phase 2 verdict: ⏸ DEFERRED — actuals check at T+24h
✓  RESOLVED                    Incident reported. Human is in the loop.
```

**Voiceover (70 sec) — say this slowly, the events animate at ~5-7 sec intervals:**

> "Sentinel is the watchman. It just checked Phoenix and found something — predictions where `api_fetch_limit=73` have a mean absolute error four times the baseline.
>
> Now Investigator wakes up. It cross-references three sources: Phoenix observability data, MLflow training lineage, and the GitHub repo. It just told us — the training run never had this `api_fetch_limit` parameter, but production is using it. That's the smoking gun.
>
> RCA Writer turns the structured agent output into a human incident report. Title, executive summary, what happened, root cause with the suspect commit, recommended fix, open questions.
>
> Remediation drafts a plan — revert commit `6270ca55`. By default it only PLANS, doesn't open the PR. Execution is gated behind explicit approval.
>
> Now Slack — Airen posts the incident card to the on-call channel. Severity-colored, full markdown, link back to the dashboard. The human gets the page.
>
> Validator runs Phase 1 — it re-queries Phoenix and confirms the bad segments are back to baseline. Phase 2 — the T+24h actuals check — is queued for tomorrow.
>
> The whole loop, from detection to validated fix, ran in under three minutes."

**Visual:** "✓ Run complete" banner appears at the bottom of the LIVE page.

---

### 🎬 SCENE 5 — The Slack message (2:00 – 2:15)

**Action:** Switch to browser tab 3 — Slack.

**Visual:** A new incident card just appeared. Red CRITICAL severity bar on the left. Show the structured sections: TL;DR, What happened, Root cause (with the PR link as a real hyperlink), Evidence bullets, Recommended fix.

**Voiceover (15 sec):**
> "Here's the Slack alert. Same content, same evidence, formatted natively. The on-call engineer's phone just buzzed with this. Click the PR link, click the dashboard link — Airen has handed off a fully-formed incident with one click to act on it."

---

### 🎬 SCENE 6 — The incident page (2:15 – 2:35)

**Action:** Click "View full incident →" in the LIVE page (or open `/incident/<run_id>`).

**Visual:** Scroll through the incident detail page. Highlight:
- The header with severity badge and TL;DR
- The "What happened" section with bullets
- The "Root cause" markdown with the commit link
- The **Remediation plan** card with the proposed PR title and rationale
- The **Validator** card with the before/after comparison table
- The **Agent timeline** with all the state transitions

**Voiceover (20 sec):**
> "This is the same incident on the web. Every state transition is recorded. Every agent's verdict is auditable. The remediation plan is here, the validator's before-and-after comparison is here. If anyone challenges the diagnosis later, the full reasoning trace is one click away."

---

### 🎬 SCENE 7 — Phoenix MCP integration (2:35 – 2:50)

**Action:** Quick switch to browser tab 4 — Phoenix UI at `localhost:6006`.

**Visual:** Show the `airen-dev` project — traces of the agent run itself. Click into one to show the AGENT → LLM → TOOL → LLM tree.

**Voiceover (15 sec):**
> "And because Airen runs on the ADK with the Phoenix MCP server wired in, every agent action is itself observable. Airen observes the model AND observes itself. The same Phoenix workspace shows both."

---

### 🎬 SCENE 8 — Close (2:50 – 3:00)

**Visual:** Closing card.

```
        AIREN — Autonomous ML Reliability Engineer

           github.com/<your-handle>/airen
                   Apache-2.0 licensed

        Built for the Google Cloud Rapid Agent Hackathon
                      Arize Track · 2026
```

**Voiceover (10 sec):**
> "Airen. From dashboard click to confirmed fix in under three minutes. Code is at the link below. Thanks for watching."

---

## Recording tips

- **Single take if possible** — the live feed naturally drives the pacing
- **Mock mode for the recording** — runs deterministically in ~2 seconds, no quota worries. The visual still shows the full state-machine trace, just compressed in time. If you want the LIVE feed to play more slowly for the camera, raise `PIPELINE_COOLDOWN_SEC=5` in `.env` before the demo so each agent step takes a beat
- **Camera:** screen recording at 1920×1080 minimum, 30 fps
- **Mic:** any USB mic; record voiceover in one pass against the b-roll
- **Cut points:** between scenes 2/3, 4/5, 6/7 — these are natural pauses
- **Music:** optional, low ambient — don't drown out the voiceover
- **Subtitles:** burn-in English subtitles for the voiceover (Devpost recommends this)

## Submission deliverables (Devpost form)

| Field | Content |
|---|---|
| Project title | Airen — Autonomous ML Reliability Engineer |
| Tagline | "From dashboard click to confirmed ML fix in under 3 minutes" |
| Hosted URL | (your Cloud Run URL after deploy.sh) |
| Demo video URL | (YouTube unlisted upload of this recording) |
| GitHub URL | (public repo with README + LICENSE) |
| Built with | Google ADK · Gemini 2.5 · Arize Phoenix · Phoenix MCP · OpenInference · FastAPI |
