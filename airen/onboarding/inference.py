"""Inference — turn a RepoManifest into a prefilled airen.yaml dict + gaps.

Everything code can reveal is prefilled. Everything code *cannot* reveal — the
healthy MAE baseline, alert thresholds, which Phoenix project is the live one,
where to post — becomes a `Gap` the agent asks the human about. The interview
never fully disappears; it shrinks to the unknowables.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from airen.onboarding.manifest import RepoManifest


@dataclass
class Gap:
    """A field the manifest couldn't determine — the agent must ask."""

    key: str                       # dotted path, e.g. "phoenix.project_name"
    prompt: str                    # what to ask the human
    default: str | None = None     # suggested default
    kind: str = "str"              # str | float | int | bool | list
    required: bool = False


@dataclass
class Inferred:
    config: dict                   # partial AirenServiceConfig dict (validated later)
    gaps: list[Gap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # human-facing "I detected X" lines


def infer_service_config(manifest: RepoManifest, service_name: str | None = None) -> Inferred:
    # ── service name ──
    if not service_name:
        if manifest.is_remote:
            service_name = manifest.source.split("/")[-1]
        else:
            service_name = manifest.source.rstrip("/").split("/")[-1]
        service_name = service_name.replace("_", "-").lower()

    notes: list[str] = []
    gaps: list[Gap] = []
    cfg: dict = {}

    # ── service identity ──
    primary_model = manifest.models[0].name if manifest.models else None
    model_kinds = sorted({m.kind for m in manifest.models})
    desc_bits = []
    if primary_model:
        desc_bits.append(primary_model)
    if model_kinds:
        desc_bits.append("/".join(model_kinds))
    if manifest.application_type != "unknown":
        desc_bits.append(manifest.application_type)
    cfg["service"] = {"name": service_name}
    if desc_bits:
        cfg["service"]["description"] = " — ".join(desc_bits)

    if manifest.models:
        notes.append(
            f"models: {', '.join(f'{m.name} ({m.kind})' for m in manifest.models[:4])}"
        )
    if manifest.frameworks:
        notes.append(f"frameworks: {', '.join(manifest.frameworks)}")
    if manifest.apis:
        ep = ", ".join(f"{a.method} {a.path}" for a in manifest.apis[:4])
        notes.append(f"serving APIs: {ep}")
        io_models = sorted({a.request_model for a in manifest.apis if a.request_model} |
                           {a.response_model for a in manifest.apis if a.response_model})
        if io_models:
            notes.append(f"I/O models: {', '.join(io_models)}")

    # ── phoenix ──
    if manifest.observability.phoenix_project:
        cfg["phoenix"] = {"project_name": manifest.observability.phoenix_project}
        notes.append(f"Phoenix project (from code): {manifest.observability.phoenix_project}")
    else:
        gaps.append(Gap(
            key="phoenix.project_name",
            prompt="Phoenix project the live service emits spans to",
            default=f"{service_name}-prediction",
            required=True,
        ))

    # ── github ──
    if manifest.is_remote:
        github: dict = {"repo": manifest.source}
        if manifest.default_branch:
            github["default_branch"] = manifest.default_branch
        if manifest.feature_constants:
            github["attributes_of_interest"] = manifest.feature_constants
            notes.append(
                f"attributes_of_interest candidates: {', '.join(manifest.feature_constants[:8])}"
            )
        cfg["github"] = github

    # ── serving topology — how the inference layer emits predictions ──
    st = manifest.serving
    if manifest.observability.uses_phoenix:
        pred_src = "phoenix"
    elif st.mode == "async-kafka":
        pred_src = "kafka"
    elif st.mode == "batch":
        pred_src = "redshift"
    else:
        pred_src = "phoenix"
    serving_cfg: dict = {"mode": st.mode, "prediction_source": pred_src}
    if st.endpoints:
        serving_cfg["endpoints"] = st.endpoints
    if st.scheduler:
        serving_cfg["scheduler"] = st.scheduler
    cfg["serving"] = serving_cfg

    topo_bits = [f"serving mode: {st.mode}"]
    if st.libraries:
        topo_bits.append(f"kafka libs: {', '.join(st.libraries)}")
    if st.kafka_topics:
        topo_bits.append(f"topics: {', '.join(st.kafka_topics)}")
    if st.scheduler:
        topo_bits.append(f"scheduler: {st.scheduler}")
    notes.append(" · ".join(topo_bits))
    notes.append(f"prediction source (live feed): {pred_src}")
    if pred_src != "phoenix":
        notes.append(
            "ⓘ Sentinel reads Phoenix today; prediction_source declares intent "
            "for the topology-aware feed (Kafka/Redshift) coming later."
        )

    if st.mode == "async-kafka":
        gaps.append(Gap(
            key="serving.output_topic",
            prompt="Kafka topic predictions land on (the live feed Airen should watch)",
            default=(st.kafka_topics[-1] if st.kafka_topics else None),
            kind="str",
        ))
        if len(st.kafka_topics) >= 2:
            gaps.append(Gap(
                key="serving.input_topic",
                prompt="Kafka topic the model consumes (input)",
                default=st.kafka_topics[0], kind="str",
            ))

    # ── observation schema — which span attrs Sentinel reads (THIS app's, not TL's) ──
    obs = manifest.observation
    observation_cfg: dict = {"problem_type": obs.problem_type}
    if obs.error_attribute:
        observation_cfg["error_attribute"] = obs.error_attribute
    if obs.segment_candidates:
        observation_cfg["segments"] = obs.segment_candidates
    if obs.problem_type == "classification":
        observation_cfg["metric_unit"] = "score"
        observation_cfg["metric_direction"] = "higher_is_better"
    cfg["observation"] = observation_cfg

    obs_bits = [f"problem type: {obs.problem_type}"]
    if obs.error_attribute:
        obs_bits.append(f"error attribute: {obs.error_attribute}")
    if obs.segment_candidates:
        obs_bits.append(f"segments: {', '.join(obs.segment_candidates)}")
    notes.append(" · ".join(obs_bits))

    # If the error attribute couldn't be found in code, Sentinel has nothing to
    # average — must ask. Default is problem-type aware (a generic placeholder,
    # not TL-ETA's), and matches what the Kafka tap writes (eval.error).
    _default_metric = obs.error_attribute or (
        "eval.is_correct" if obs.problem_type == "classification" else "eval.error"
    )
    gaps.append(Gap(
        key="observation.error_attribute",
        prompt="Span attribute holding the per-prediction error/score Sentinel averages",
        default=_default_metric,
        required=True,
    ))

    # ── mlflow / model registry from tracking ──
    if manifest.tracking.uses_mlflow:
        exp = manifest.tracking.experiment_names[0] if manifest.tracking.experiment_names else None
        cfg["mlflow"] = {
            "tracking_uri": None,   # fill in airen.yaml, e.g. https://mlflow-dev.fourkites.com
            "experiment_name": exp,
            "enable": False,
        }
        if exp:
            notes.append(f"MLflow experiment (from code): {exp}")

    if manifest.models:
        registry = []
        for m in manifest.models[:6]:
            registry.append({
                "model_family": m.name,
                "experiment_id": "TBD",  # code can't know the MLflow id
                "expected_repo": manifest.source if manifest.is_remote else None,
                "notes": f"{m.kind} via {m.framework or 'unknown'} ({m.file}:{m.line})",
            })
        cfg["model_registry"] = registry
        gaps.append(Gap(
            key="_model_registry_ids",
            prompt=(
                "MLflow experiment id(s) for the detected models "
                "(blank to leave as 'TBD' in the yaml)"
            ),
            kind="str",
        ))

    # ── sentinel: code can't tell us what 'healthy' is ──
    gaps.extend([
        Gap("sentinel.baseline_mae_minutes",
            "Healthy baseline error (MAE, in minutes) — code can't infer this",
            default="150", kind="float"),
        Gap("sentinel.critical_ratio",
            "CRITICAL when current/baseline ratio ≥", default="2.0", kind="float"),
    ])

    # ── reporting destinations ──
    gaps.append(Gap("slack.alert_channel",
                    "Slack alert channel (blank → global SLACK_CHANNEL env)",
                    default=None, kind="str"))
    gaps.append(Gap("jira.project_key",
                    "Jira project key for incidents (blank → Jira disabled)",
                    default=None, kind="str"))

    return Inferred(config=cfg, gaps=gaps, notes=notes)
