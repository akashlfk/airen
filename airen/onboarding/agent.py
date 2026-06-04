"""Onboarding agent — graphify a repo, prefill the config, interview the gaps.

Flow:
  1. graphify the repo (deterministic) → RepoManifest, saved to graphs/
  2. show the human what was detected (model type, frameworks, APIs, I/O)
  3. infer as much of airen.yaml as code allows
  4. interview ONLY the gaps (thresholds, live Phoenix project, destinations)
  5. validate against AirenServiceConfig, write services/<name>/airen.yaml

The detection is fully deterministic; the optional LLM step only writes a
one-line human narrative of the repo (skipped in mock mode / on any error),
honoring Airen's "tools decide, LLM narrates" rule.
"""

from __future__ import annotations

from pathlib import Path

from airen.config import SERVICES_DIR, AirenServiceConfig
from airen.onboarding import io
from airen.onboarding.inference import Gap, infer_service_config
from airen.onboarding.manifest import RepoManifest
from airen.onboarding.scan import graphify, graphify_local


# ───────────────────────────────────────────────────────────────────────
#  dotted-key setter
# ───────────────────────────────────────────────────────────────────────
def _set(cfg: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = cfg
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _coerce(gap: Gap, raw: str | None):
    if raw is None or raw == "":
        return None
    if gap.kind == "float":
        try:
            return float(raw)
        except ValueError:
            return None
    if gap.kind == "int":
        try:
            return int(raw)
        except ValueError:
            return None
    return raw


# ───────────────────────────────────────────────────────────────────────
#  manifest summary
# ───────────────────────────────────────────────────────────────────────
def print_manifest(manifest: RepoManifest, notes: list[str]) -> None:
    io.section("Graphify — what Airen detected", f"source: {manifest.source}")
    print(f"   application type : {manifest.application_type}")
    print(f"   languages        : {', '.join(manifest.languages) or '—'}")
    print(f"   frameworks       : {', '.join(manifest.frameworks) or '—'}")
    if manifest.commit_sha:
        print(f"   commit           : {manifest.commit_sha[:12]}")
    for n in notes:
        print(f"   • {n}")
    if not manifest.models and not manifest.apis:
        print("   ⚠ no models or serving APIs detected — you may need to fill these by hand.")


def maybe_llm_summary(manifest: RepoManifest) -> str | None:
    """One-line narrative via the LLM. Best-effort; returns None on any issue."""
    from airen.mocks import is_mock_mode

    if is_mock_mode():
        return None
    try:
        from airen.llm_factory import get_llm  # type: ignore

        facts = (
            f"app_type={manifest.application_type}; frameworks={manifest.frameworks}; "
            f"models={[m.name for m in manifest.models]}; "
            f"apis={[(a.method, a.path) for a in manifest.apis]}"
        )
        llm = get_llm()
        resp = llm.complete(  # type: ignore[attr-defined]
            "In one sentence, describe what this ML service does. Facts: " + facts
        )
        return str(resp).strip().split("\n")[0][:200]
    except Exception:
        return None


# ───────────────────────────────────────────────────────────────────────
#  the agent
# ───────────────────────────────────────────────────────────────────────
def onboard_from_repo(
    repo_or_path: str,
    *,
    service_name: str | None = None,
    is_local: bool = False,
    interactive: bool = True,
    force: bool = False,
    use_llm: bool = True,
) -> Path | None:
    """Run the full agentic onboarding. Returns the written path, or None."""
    # 1. graphify
    print("\n  ⏳ graphifying repo… (clone/refresh + deterministic scan)")
    manifest = graphify_local(repo_or_path) if is_local else graphify(repo_or_path)

    # 2. infer
    inferred = infer_service_config(manifest, service_name)
    if use_llm:
        summary = maybe_llm_summary(manifest)
        if summary:
            manifest.summary = summary
            manifest.save()
            inferred.notes.insert(0, f"summary: {summary}")

    print_manifest(manifest, inferred.notes)
    cfg = inferred.config
    name = cfg["service"]["name"]

    # 3. confirm inferred fields + interview gaps
    if interactive:
        io.section(
            "A few details I couldn't read from the code",
            "I auto-detected everything above. Please fill in the rest below — "
            "press Enter to accept any [default] shown in brackets.",
        )
        name = io.ask_required("Service name", name)
        cfg["service"]["name"] = name

        # Operator context fed to the Investigator + RCA agents (out-of-band
        # knowledge the repo doesn't contain).
        ctx = io.ask(
            "Operator context for the agents? (upstream deps, known issues, "
            "retrain/approval policy — blank to skip)", None,
        )
        if ctx:
            cfg["service"]["context"] = ctx

        # Live probe: sample the real source to auto-detect fields → tap.field_map
        # + observation. Prompts for any connection info it needs first.
        src = (cfg.get("serving") or {}).get("prediction_source", "phoenix")
        if io.ask_bool(f"Probe the live {src} source now to auto-detect its fields?", default=False):
            try:
                _probe_and_apply(cfg)
            except Exception as e:  # noqa: BLE001
                print(f"  (probe skipped: {type(e).__name__}: {e})")

        # let the human curate the auto-detected attributes_of_interest
        gh = cfg.get("github")
        if gh and gh.get("attributes_of_interest"):
            gh["attributes_of_interest"] = io.ask_list(
                "attributes_of_interest (Investigator watches these)",
                default=gh["attributes_of_interest"],
            )

        # let the human curate the detected segmentation dimensions
        obs_cfg = cfg.get("observation")
        if obs_cfg and obs_cfg.get("segments"):
            obs_cfg["segments"] = io.ask_list(
                "observation.segments (span attrs Sentinel groups health by)",
                default=obs_cfg["segments"],
            )

        # PSI drift baselines — training-time expected value per attribute. Code
        # can detect the attribute NAMES but not their training values, so we ask.
        attrs = (cfg.get("github") or {}).get("attributes_of_interest") or []
        hint = ", ".join(f"{a}=" for a in attrs[:3]) if attrs else "api_fetch_limit=184, seq_len=48"
        print("\n  Drift baselines (PSI) — training-time expected value per attribute.")
        print(f"  Enter key=value pairs (e.g. {hint}) or press Enter to skip:")
        raw = input("  attribute baselines: ").strip()
        if raw:
            baselines: dict[str, str] = {}
            for pair in raw.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k.strip() and v.strip():
                        baselines[k.strip()] = v.strip()
            if baselines:
                cfg.setdefault("sentinel", {})["attribute_baselines"] = baselines

        for gap in inferred.gaps:
            if gap.key == "_model_registry_ids":
                _apply_model_ids(cfg, gap)
                continue
            if gap.key == "jira.project_key":
                _apply_jira(cfg, gap)
                continue
            raw = io.ask(gap.prompt, gap.default)
            val = _coerce(gap, raw)
            if gap.key == "slack.alert_channel":
                cfg.setdefault("slack", {})["enable"] = True
                cfg["slack"]["alert_channel"] = val  # may be None → env fallback
            elif val is not None:
                _set(cfg, gap.key, val)
    else:
        # non-interactive: apply gap defaults where present
        for gap in inferred.gaps:
            if gap.key.startswith("_") or gap.key == "jira.project_key":
                continue
            val = _coerce(gap, gap.default)
            if val is not None:
                _set(cfg, gap.key, val)

    # 4. validate
    try:
        AirenServiceConfig.model_validate(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ Inferred config failed validation: {type(e).__name__}: {e}")
        print("   Nothing written. Re-run and adjust, or use `python -m airen.run_onboard` manually.\n")
        return None

    # 5. write
    out_dir = SERVICES_DIR / name
    out_path = out_dir / "airen.yaml"
    if out_path.exists() and not force:
        if interactive:
            if not io.ask_bool(f"\n{out_path} exists. Overwrite?", default=False):
                print("✗ Left existing config untouched.\n")
                return None
        else:
            print(f"✗ {out_path} exists (use force=True to overwrite).")
            return None

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(io.render_yaml(cfg, name, generated_by=f"python -m airen.run_onboard --repo {manifest.source}"))
    print()
    print("═" * 78)
    print(f"  ✅ Wrote {out_path}")
    print(f"  📊 Manifest cached at {manifest.path()}")
    print("═" * 78)

    # Secrets side: report required .env keys and scaffold the missing ones.
    from airen.onboarding.preflight import preflight_report

    validated = AirenServiceConfig.model_validate(cfg)
    preflight_report(validated, scaffold_env=interactive)
    print("\n  ── Next steps " + "─" * 64)
    print(f"    1. Review the config:        {out_path}")
    print(f"    2. Fill in your secrets:     .env  (the keys listed above)")
    print(f"    3. Calibrate on live data:   python -m airen.run_calibrate {name}")
    print(f"    4. Try it with NO LLM/creds: AIREN_LLM_MODE=mock python -m airen.run_orchestrator {name}")
    print(f"    5. Run for real:             python -m airen.run_orchestrator {name}\n")

    # Offer calibration now — only useful if the service is already emitting.
    if interactive and io.ask_bool(
        "Run a calibration against live data now? (needs your service emitting predictions)",
        default=False,
    ):
        from airen.onboarding.calibrate import calibrate

        try:
            calibrate(validated, interactive=True)
        except Exception as e:  # noqa: BLE001
            print(f"  (calibration skipped: {type(e).__name__}: {e})")
    return out_path


def _probe_and_apply(cfg: dict) -> None:
    """Prompt for connection info, probe the live source, infer field roles, and
    (with confirmation) write tap.field_map + observation into the config."""
    from airen.config import AirenServiceConfig
    from airen.onboarding.probe import infer_field_roles, probe_source, required_connection_info

    # The Kafka dispatcher reads kafka.output_topic — mirror serving.output_topic.
    serving = cfg.get("serving") or {}
    if serving.get("prediction_source") == "kafka" and serving.get("output_topic"):
        cfg.setdefault("kafka", {}).setdefault("output_topic", serving["output_topic"])

    def _working_config():
        try:
            return AirenServiceConfig.model_validate(cfg)
        except Exception as e:  # noqa: BLE001
            print(f"  (can't probe — config not valid yet: {e})")
            return None

    wc = _working_config()
    if wc is None:
        return

    # Prompt for whatever's still needed to REACH the source.
    for keypath, prompt in required_connection_info(wc):
        if keypath.startswith("env:"):
            print(f"  ⓘ Need {keypath[4:]} in .env to probe — set it then re-run, or skip.")
            continue
        val = io.ask(prompt, None)
        if val:
            blk, fld = keypath.split(".", 1)
            cfg.setdefault(blk, {})[fld] = val
    wc = _working_config() or wc

    print("  ⏳ probing the live source (read-only sample)…")
    observed = probe_source(wc)
    if observed is None:
        print("  ⚠ couldn't reach the source / no data — skipping. Fill tap.field_map "
              "by hand, or re-run onboarding on a host that can reach it (e.g. the EC2 box).")
        return

    prop = infer_field_roles(observed)
    print(f"  ✓ sampled {observed.n_sampled} records. Auto-detected:")
    for n in prop.notes:
        print(f"     • {n}")
    for a in prop.ambiguities:
        print(f"     ⚠ {a}")
    if io.ask_bool("  Apply this mapping (tap.field_map + observation)?", default=True):
        cfg["tap"] = {"field_map": prop.tap_field_map}
        cfg.setdefault("observation", {}).update(prop.observation)
        print("  ✓ applied — you can still tweak observation.segments below.")


def _apply_model_ids(cfg: dict, gap: Gap) -> None:
    registry = cfg.get("model_registry") or []
    if not registry:
        return
    raw = io.ask(gap.prompt + f" (in order: {', '.join(e['model_family'] for e in registry)})", None)
    if not raw:
        return
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    for entry, exp_id in zip(registry, ids):
        entry["experiment_id"] = exp_id


def _apply_jira(cfg: dict, gap: Gap) -> None:
    key = io.ask(gap.prompt, gap.default)
    if not key:
        cfg["jira"] = {"enable": False}
        return
    cfg["jira"] = {
        "enable": True,
        "project_key": key,
        "issue_type": io.ask("  Jira issue type (must exist in the project)", "Task"),
    }
