# Airen on an EC2 box — conda-native (no Docker)

For a Linux box that has **conda** and can reach **Kafka** (internal real-data
track). 30 GB RAM / 260 GB free is plenty — Phoenix on **SQLite** (default) will
hold millions of spans on that disk, so **no Postgres needed** for the PoC.

> ⚠️ AWS hosting here is the **internal** real-data track only. The hackathon
> **submission stays AWS-free** (GCP/local + mock/clean adapters).

The four long-running processes:
```
 your model ─Kafka(unchanged)─▶ airen-tap ─spans─▶ phoenix (SQLite @ ~/.phoenix)
                                                       │
                                airen-orch ─reads──────┘  (Sentinel→Investigator→…)
                                    │
                                airen-web ─▶ http://<host>:8000
```

## 1. Create the env + install
```bash
git clone <your-airen-repo> ~/airen && cd ~/airen
conda create -n mlre python=3.11 -y
conda activate mlre
pip install -r requirements.txt
# Node is needed for the Phoenix MCP (Arize requirement):
#   (Amazon Linux) sudo dnf install -y nodejs   — or use nvm
which python        # ← note this path; you'll need it for the systemd units
```

## 2. Secrets + config
```bash
cp .env.example .env
# Fill the values THIS box can reach:
#   GOOGLE_API_KEY, GITHUB_TOKEN
#   KAFKA_SASL_USERNAME / KAFKA_SASL_PASSWORD     (topology lives in the yaml)
#   AIREN_KAFKA_MODE=real
#   PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006
#   PHOENIX_WORKING_DIR=/home/<user>/airen/.phoenix    # SQLite lives here
#   PHOENIX_PROJECT_NAME_TAP=<your-service>-prediction  # = phoenix.project_name
```

## 3. Onboard your service once (interactive)
```bash
python -m airen.run_onboard --repo <owner>/<repo> --name <service>
cat services/<service>/airen.yaml      # review: prediction_source: phoenix, project_name, observation.*
```

## 4a. Quick start (tmux — to test it right now)
```bash
tmux new -s airen
# pane 1:
phoenix serve
# Ctrl+b " (split), pane 2:
python -m airen.run_kafka_tap --mode real --max 10000 --loop 300
# pane 3:
python -m airen.run_orchestrator <service> --loop 600
# pane 4:
python -m airen.run_web
# detach: Ctrl+b d   (processes keep running)
```
Open `http://<host>:8000` (Airen) and `http://<host>:6006` (Phoenix). Open those
ports to your IP only in the security group.

## 4b. Proper (systemd — auto-restart + survives reboot)
Unit files are in `deploy/ec2/systemd/`. Fill the placeholders and install:
```bash
# Replace tokens in all 4 units (one sed), then install:
export AIREN_REPO=$HOME/airen
export AIREN_PY=$(which python)              # the mlre env's python
export AIREN_USER=$(whoami)
export AIREN_SERVICE=<your-service>
for f in deploy/ec2/systemd/*.service; do
  sed -e "s#__REPO__#$AIREN_REPO#g" -e "s#__PY__#$AIREN_PY#g" \
      -e "s#__USER__#$AIREN_USER#g" -e "s#__SERVICE__#$AIREN_SERVICE#g" \
      "$f" | sudo tee /etc/systemd/system/$(basename "$f") >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now airen-phoenix airen-tap airen-orchestrator airen-web
```
Check + logs:
```bash
systemctl status airen-orchestrator
journalctl -u airen-tap -f          # watch spans flow to Phoenix
journalctl -u airen-orchestrator -f
```

## 5. Calibrate once data is flowing
```bash
python -m airen.run_calibrate <service>
```

## Tunables
| Knob | Where | Default |
|---|---|---|
| tap batch size | `--max` in airen-tap.service | 10000 |
| tap interval | `--loop` in airen-tap.service | 300s |
| monitor interval | `--loop` in airen-orchestrator.service | 600s |
| SQLite location | `PHOENIX_WORKING_DIR` in .env | ~/.phoenix |

## Safety
The orchestrator runs **without `--execute`** → it prepares PRs + posts to Slack
but **never merges**. To enable the full merge→deploy→validate loop, add
`--execute` to airen-orchestrator.service and set `remediation.auto_merge: true`
in the service yaml.
