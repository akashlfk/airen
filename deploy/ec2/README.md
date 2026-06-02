# Deploying Airen on a Linux box (EC2 / FK VM) — the internal real-data track

Run this where **Kafka and Redshift are reachable** (your laptop can't reach them
on the split-tunnel VPN, so we run on an instance inside the network). This is
the **FK-real / internal demo track**.

> ⚠️ **Hackathon compliance:** AWS must stay out of the *submission*. This EC2
> deployment is for the **internal** real-data demo only. The public submission
> runs on **GCP/local** with the mock/clean adapters. Don't submit this stack.

The whole thing is one `docker compose` — Phoenix + Postgres + the Airen web UI +
the Kafka→Phoenix tap + the monitoring loop.

```
 your model ──Kafka (unchanged)──▶ airen-tap ──spans──▶ phoenix(+postgres)
                                                            │
                                   airen-orch ──reads──────┘ (Sentinel→Investigator→…)
                                       │
                                   airen-web  ──▶  http://<host>:8000
```

## 1. Prerequisites on the box
```bash
# Docker + compose plugin (Amazon Linux 2023 example)
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker
docker compose version   # confirm the compose plugin is present
```

## 2. Get the code + secrets
```bash
git clone <your-airen-repo> airen && cd airen
cp .env.example .env
# Edit .env with the REAL values this box can reach:
#   GOOGLE_API_KEY=...                 (or AIREN_LLM_BACKEND=azure for dev)
#   GITHUB_TOKEN=...
#   KAFKA_SASL_USERNAME / KAFKA_SASL_PASSWORD   (topology lives in the yaml)
#   SLACK_BOT_TOKEN, AIREN_JIRA_API_TOKEN (optional)
#   AIREN_KAFKA_MODE=real
#   AIREN_SERVICE=<your-onboarded-service-name>
# Phoenix/Postgres are wired by compose — you do NOT set PHOENIX_COLLECTOR_ENDPOINT
# (compose overrides it to http://phoenix:6006).
```

## 3. Onboard your service once (interactive)
```bash
docker compose run --rm airen-web python -m airen.run_onboard --repo <owner>/<repo> --name <service>
docker compose run --rm airen-web cat services/<service>/airen.yaml   # review
```
Set `serving.prediction_source: phoenix` (the tap puts predictions in Phoenix) and
make sure `phoenix.project_name` matches `PHOENIX_PROJECT_NAME_TAP` (the tap's target).

## 4. Bring up the whole stack
```bash
docker compose up -d --build
docker compose ps            # all healthy?
docker compose logs -f airen-tap     # watch spans flow to Phoenix
```
Open:
- **Airen dashboard:** `http://<host>:8000`
- **Phoenix UI:** `http://<host>:6006`

(Open ports 8000 + 6006 to your IP in the security group; keep them off the public internet.)

## 5. Calibrate once data is flowing
```bash
docker compose run --rm airen-orch python -m airen.run_calibrate <service>
```

## Tunables (set in .env)
| Var | Default | Meaning |
|---|---|---|
| `AIREN_SERVICE` | — (required) | which onboarded service the orchestrator monitors |
| `TAP_MAX` | 10000 | messages per tap pass |
| `TAP_LOOP` | 300 | seconds between tap passes (300=5m, 600=10m, 3600=60m) |
| `ORCH_LOOP` | 600 | seconds between monitoring cycles |
| `AIREN_KAFKA_MODE` | real | `real` peeks the broker; `mock` synthesizes (no broker) |
| `PHOENIX_PG_PASSWORD` | phoenix | Postgres password for Phoenix persistence |

## Safety
- The orchestrator runs **without `--execute`**, so it prepares PRs + posts to
  Slack but **never merges**. To enable the merge loop later, add `--execute` to
  `airen-orch`'s command and set `remediation.auto_merge: true` in the service yaml.
- Phoenix is **Postgres-backed** (volume `pgdata`) → no span cap, survives restarts.

## Common commands
```bash
docker compose restart airen-orch     # restart just the monitor
docker compose logs -f airen-orch     # tail the pipeline
docker compose down                   # stop all (keeps pgdata volume)
docker compose down -v                # stop + wipe Phoenix data
```
