"""Incident store for the web UI — file-based, read-mostly.

Reads `logs/INC-*.json` written by `airen/run_orchestrator.py`. No in-memory
state to keep in sync between CLI and web — the filesystem IS the store.

Each StoredIncident wraps an IncidentRun. Backward-compatible properties
(`incident_id`, `service_name`, `report`, `sentinel`, `investigator`, `created_at`)
let existing Jinja templates keep working unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from airen.mocks import (
    mock_incident_report,
    mock_investigator_verdict,
    mock_sentinel_verdict,
)
from airen.schemas import (
    HealthStatus,
    IncidentEvent,
    IncidentReport,
    IncidentRun,
    InvestigatorVerdict,
    OrchestratorState,
    SentinelVerdict,
)

# logs/ lives one level up from the airen/ package
LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "logs"


class StoredIncident:
    """Wraps an IncidentRun with template-friendly accessor properties."""

    def __init__(self, run: IncidentRun) -> None:
        self.run = run

    # ── identity ──────────────────────────────────────────────────
    @property
    def incident_id(self) -> str:
        return self.run.run_id

    @property
    def service_name(self) -> str:
        return self.run.project_name

    # ── time ──────────────────────────────────────────────────────
    @property
    def created_at(self) -> datetime:
        try:
            return datetime.fromisoformat(self.run.started_at)
        except (ValueError, TypeError):
            return datetime.now(timezone.utc)

    @property
    def finished_at(self) -> datetime | None:
        if not self.run.finished_at:
            return None
        try:
            return datetime.fromisoformat(self.run.finished_at)
        except (ValueError, TypeError):
            return None

    @property
    def duration_seconds(self) -> float | None:
        fin = self.finished_at
        if fin is None:
            return None
        return (fin - self.created_at).total_seconds()

    # ── agent outputs ─────────────────────────────────────────────
    @property
    def report(self) -> IncidentReport | None:
        return self.run.incident_report

    @property
    def sentinel(self) -> SentinelVerdict | None:
        return self.run.sentinel_verdict

    @property
    def investigator(self) -> InvestigatorVerdict | None:
        return self.run.investigator_verdict

    @property
    def events(self) -> list[IncidentEvent]:
        return self.run.events

    @property
    def final_state(self) -> OrchestratorState:
        return self.run.final_state

    @property
    def slack_permalink(self) -> str | None:
        return self.run.slack_permalink


# ───────────────────────────────────────────────────────────────────────
#  Reading from disk
# ───────────────────────────────────────────────────────────────────────
def _load_run(path: Path) -> StoredIncident | None:
    try:
        data = json.loads(path.read_text())
        run = IncidentRun.model_validate(data)
        return StoredIncident(run)
    except Exception:
        return None  # corrupt or partial; skip silently


def _scan_logs() -> list[StoredIncident]:
    if not LOGS_DIR.exists():
        return []
    incidents: list[StoredIncident] = []
    for p in LOGS_DIR.glob("INC-*.json"):
        s = _load_run(p)
        if s is not None:
            incidents.append(s)
    return incidents


# ───────────────────────────────────────────────────────────────────────
#  Public API (template-friendly)
# ───────────────────────────────────────────────────────────────────────
def list_incidents() -> list[StoredIncident]:
    """All recorded runs, newest first. Includes both completed and failed runs."""
    out = _scan_logs()
    out.sort(key=lambda i: i.created_at, reverse=True)
    return out


def get_incident(incident_id: str) -> StoredIncident | None:
    """Look up one run by its run_id (= incident_id)."""
    path = LOGS_DIR / f"{incident_id}.json"
    if path.exists():
        return _load_run(path)
    # Fallback — scan in case the file naming changed
    for s in _scan_logs():
        if s.incident_id == incident_id:
            return s
    return None


def list_services() -> list[dict]:
    """Fleet summary — one row per distinct service, aggregating its runs."""
    by_service: dict[str, dict] = {}
    sev_rank = {"HEALTHY": 0, "WARNING": 1, "CRITICAL": 2}
    for inc in list_incidents():
        if inc.report is None:
            continue  # don't surface failed runs on the fleet view
        s = by_service.setdefault(
            inc.service_name,
            {
                "name": inc.service_name,
                "open_incidents": 0,
                "worst_severity": "HEALTHY",
                "latest_summary": "",
                "latest_at": None,
            },
        )
        s["open_incidents"] += 1
        if sev_rank[inc.report.severity.value] > sev_rank[s["worst_severity"]]:
            s["worst_severity"] = inc.report.severity.value
            s["latest_summary"] = inc.report.tldr
        if s["latest_at"] is None or inc.created_at > s["latest_at"]:
            s["latest_at"] = inc.created_at
    return list(by_service.values())


def seed_with_mock_data() -> None:
    """If no real runs exist on disk yet, write one mock IncidentRun so the UI
    has something to show on first boot."""
    if list_incidents():
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = mock_sentinel_verdict()
    investigator = mock_investigator_verdict(sentinel=sentinel)
    report = mock_incident_report(sentinel=sentinel, investigator=investigator)
    seed_run = IncidentRun(
        run_id="INC-DEMO01",
        project_name="tl-eta-prediction",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        final_state=OrchestratorState.RESOLVED,
        sentinel_verdict=sentinel,
        investigator_verdict=investigator,
        incident_report=report,
        events=[
            IncidentEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                state=OrchestratorState.RESOLVED,
                agent=None,
                message="Demo seed — mock run for first-boot UI",
            )
        ],
    )
    (LOGS_DIR / "INC-DEMO01.json").write_text(
        json.dumps(seed_run.model_dump(mode="json"), indent=2)
    )
