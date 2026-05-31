# Slack setup — enabling the Concierge conversational agent

Two-way Slack interaction needs a public HTTPS URL Slack can POST to.
Three deployment paths, easiest to hardest:

| Path | Use when | Setup time |
|---|---|---|
| **ngrok** (laptop dev) | Iterating locally before submission | ~3 min |
| **Cloud Run** (hackathon submission) | Going live | ~10 min |
| **Render / Fly / HuggingFace Spaces** | Cloud Run blocked | ~10 min |

This guide assumes you've already done the basic Slack app setup (one-way
posts working — see the main README). What follows is the *additional*
setup for the Concierge.

---

## 1. Add Events API permissions to your Slack app

1. Go to https://api.slack.com/apps → your Airen app
2. Sidebar → **OAuth & Permissions** → **Bot Token Scopes** → add:
   - `app_mentions:read` — see when someone @-mentions Airen
   - `im:history` — read direct messages sent to the bot
   - `im:read` — list DM channels
   - `im:write` — reply in DMs
3. Sidebar → **Event Subscriptions** → toggle **Enable Events** ON
4. **Request URL**: paste your public HTTPS URL + `/slack/events`
   (e.g. `https://abc123.ngrok.io/slack/events` or `https://airen-xxx.run.app/slack/events`)
   - Slack will POST a `url_verification` challenge — Airen echoes it back automatically
   - You should see a green ✓ "Verified" check after a few seconds
5. Under **Subscribe to bot events** add:
   - `app_mention`
   - `message.im`
6. Click **Save Changes** at the bottom
7. Sidebar → **Install App** → Slack will prompt to **Reinstall** to grant
   the new scopes. Authorize.

## 2. Add the signing secret to `.env`

1. Sidebar → **Basic Information** → **App Credentials** → **Signing Secret**
2. Click "Show" → copy
3. In `.env`:
   ```
   SLACK_SIGNING_SECRET=<the value>
   ```

## 3. Pick a deployment path

### Option A — ngrok (laptop dev)

Quickest. Use this to iterate on the Concierge before final deploy.

```bash
# install ngrok if you don't have it
brew install ngrok

# start your web server
AIREN_LLM_MODE=mock python -m airen.run_web
# (in another terminal)
ngrok http 8000

# copy the https URL ngrok prints (e.g. https://1a2b3c.ngrok.io)
# paste into Slack app's Event Subscriptions Request URL (with /slack/events suffix)
```

### Option B — Cloud Run

```bash
GCP_PROJECT=airen-hackathon-2026 ./deploy/cloud-run/deploy.sh
# Get the Cloud Run URL printed at the end
# Paste <url>/slack/events into Slack's Event Subscriptions
```

### Option C — Render / Fly / HuggingFace Spaces

The `Dockerfile` in repo root is universal. Whichever host you pick:
1. Connect your GitHub repo
2. Point at the `Dockerfile`
3. Set the env vars (SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, GOOGLE_API_KEY, etc.)
4. Deploy. Use the host's URL + `/slack/events`.

---

## 4. Test the Concierge

In your Slack workspace:

```
You: @Airen status of tl-eta

Airen: 🔴 *CRITICAL* — tl-eta-prediction
       Last run: `INC-A3F2B7` (2 min ago)
       Worst anomaly: api_fetch_limit=73 at 4.21× baseline.
       Full report: https://airen-xxx.run.app/incident/INC-A3F2B7
```

Or in a DM:

```
You: what happened with INC-A3F2B7

Airen: 🔴 *INC-A3F2B7 — TL ETA: training/inference mismatch …*
       The TL ETA model is producing predictions ~4× more inaccurate than
       usual for a specific slice of long-haul loads. Likely cause: a
       recent LocationService change that truncates ping history.
       <https://airen-xxx.run.app/incident/INC-A3F2B7|View full incident>
       🎫 <https://fourkites.atlassian.net/browse/ETAI-1234|ETAI-1234>
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Slack shows "Your URL didn't respond to the challenge parameter" | The webhook server isn't reachable. ngrok tunnel down, or Cloud Run not deployed. |
| Slack says "Request URL didn't return HTTP 200" | Likely a signing-secret mismatch. Either set `SLACK_SIGNING_SECRET` correctly, or for ngrok dev set `AIREN_SLACK_SKIP_VERIFY=1` (NEVER in production). |
| Bot doesn't respond to @-mentions | Reinstall the app after adding scopes; invite the bot to the channel; check `event_callback` events are reaching the server (look at server logs). |
| Bot replies to its own messages (loops) | The handler already filters `bot_id` / `subtype=bot_message`, but if you renamed the bot, double-check. |
