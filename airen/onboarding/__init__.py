"""Airen onboarding — point Airen at an ML application it has never seen.

Two front doors, one output (`services/<name>/airen.yaml`):

  • Manual interview  →  airen.onboarding.io.interview() + render_yaml()
                         (`python -m airen.run_onboard`)
  • Agentic from repo →  airen.onboarding.agent.onboard_from_repo()
                         (`python -m airen.run_onboard --repo owner/repo`)

Layers
------
  1. graphify (scan.py)      deterministic repo → RepoManifest
  2. inference (inference.py) RepoManifest → prefilled AirenServiceConfig + gaps
  3. agent (agent.py)         confirm inferred fields, interview only the gaps,
                              validate, write the yaml
  + freshness (freshness.py)  poll HEAD each orchestrator cycle, re-graphify on change
"""
