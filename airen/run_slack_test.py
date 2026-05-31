"""Post the mock IncidentReport to Slack — smoke test for the Slack adapter.

Usage:
    python -m airen.run_slack_test
    python -m airen.run_slack_test "#some-other-channel"

What it does:
    1. Builds the canned mock IncidentReport (same one the UI shows at /incident/INC-001)
    2. Posts it to SLACK_CHANNEL via the adapter
    3. Prints the permalink so you can click straight into the message

Zero Gemini calls. Just exercises the Slack adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.adapters.slack import post_incident_report
from airen.mocks import (
    mock_incident_report,
    mock_investigator_verdict,
    mock_sentinel_verdict,
)


def main() -> None:
    channel_override = sys.argv[1] if len(sys.argv) > 1 else None

    sentinel = mock_sentinel_verdict()
    investigator = mock_investigator_verdict(sentinel=sentinel)
    report = mock_incident_report(sentinel=sentinel, investigator=investigator)

    print(f"Posting mock IncidentReport ({report.severity.value}) to Slack…")
    result = post_incident_report(
        report,
        channel=channel_override,
        incident_id="INC-001",
    )
    print(f"  → ok={result.get('ok')}")
    if result.get("ok"):
        print(f"  → channel: {result.get('channel')}")
        print(f"  → ts:      {result.get('ts')}")
        if result.get("permalink"):
            print(f"  → permalink: {result.get('permalink')}")
        print("\n✓ Posted successfully. Check Slack.")
    else:
        print(f"  → error: {result.get('error')}")
        if result.get("details"):
            print(f"  → details: {result.get('details')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
