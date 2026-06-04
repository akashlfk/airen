# services/ — your onboarded services live here

This is the **production registry**. Each real service you onboard gets its own
`services/<name>/airen.yaml`, written by:

    python -m airen.run_onboard --repo <owner>/<repo> --name <service>

It starts **empty by design** — no demo data in the production path.

The demo fixtures (`tl-eta`, `ocean-eta`) live under `demo/services/`. They're
resolvable by name for mock-mode and tests, but are **not** shown by
`python -m airen.run_orchestrator --list`.
