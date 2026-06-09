"""Deterministic repo detectors — the scanners behind graphify.

Each detector is a pure function over a local repo `Path`. They use stdlib
`ast` for Python files and tolerant regex for everything else. Every detector
is defensive: a file that won't parse is skipped, never fatal. No network, no
LLM, no ripgrep — just the filesystem and the standard library.

The principle (per Airen's architecture): **detection is deterministic.** The
LLM never decides what the model type is; these functions do, and the LLM only
narrates the result.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

# Directories we never descend into.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", "venv", ".venv", "env", ".env", "site-packages",
    "build", "dist", ".tox", ".eggs", "egg-info", ".idea", ".vscode",
    "vendor", "third_party", ".next", "target",
}

# Dependency name → (human framework label, model "kind" it implies or None).
_FRAMEWORK_MAP: dict[str, tuple[str, str | None]] = {
    "torch": ("PyTorch", "deep-learning"),
    "pytorch": ("PyTorch", "deep-learning"),
    "pytorch-lightning": ("PyTorch Lightning", "deep-learning"),
    "lightning": ("PyTorch Lightning", "deep-learning"),
    "tensorflow": ("TensorFlow", "deep-learning"),
    "keras": ("Keras", "deep-learning"),
    "transformers": ("HuggingFace Transformers", "deep-learning"),
    "scikit-learn": ("scikit-learn", "tree-ensemble"),
    "sklearn": ("scikit-learn", "tree-ensemble"),
    "xgboost": ("XGBoost", "tree-ensemble"),
    "lightgbm": ("LightGBM", "tree-ensemble"),
    "catboost": ("CatBoost", "tree-ensemble"),
    "prophet": ("Prophet", "timeseries"),
    "statsmodels": ("statsmodels", "timeseries"),
    "fastapi": ("FastAPI", None),
    "flask": ("Flask", None),
    "uvicorn": ("Uvicorn", None),
    "gunicorn": ("Gunicorn", None),
    "mlflow": ("MLflow", None),
    "arize-phoenix": ("Arize Phoenix", None),
    "phoenix": ("Arize Phoenix", None),
    "openinference-instrumentation": ("OpenInference", None),
    "opentelemetry-sdk": ("OpenTelemetry", None),
    "pandas": ("pandas", None),
    "numpy": ("NumPy", None),
}

# sklearn / boosting estimator class names we recognize on sight.
_ESTIMATOR_NAMES = {
    "RandomForestClassifier": "tree-ensemble", "RandomForestRegressor": "tree-ensemble",
    "GradientBoostingClassifier": "tree-ensemble", "GradientBoostingRegressor": "tree-ensemble",
    "XGBClassifier": "tree-ensemble", "XGBRegressor": "tree-ensemble",
    "LGBMClassifier": "tree-ensemble", "LGBMRegressor": "tree-ensemble",
    "CatBoostClassifier": "tree-ensemble", "CatBoostRegressor": "tree-ensemble",
    "LogisticRegression": "linear", "LinearRegression": "linear", "Ridge": "linear", "Lasso": "linear",
    "Sequential": "deep-learning",
}

# Feature-constant heuristic: module-level numeric assignments whose name
# contains one of these stems are likely tunable knobs worth watching.
_FEATURE_STEMS = (
    "limit", "len", "size", "dim", "_k", "pings", "window", "threshold",
    "batch", "epoch", "rate", "max_", "min_", "n_", "seq", "lookback",
    "horizon", "topk", "top_k", "depth", "estimators", "features",
)


# ───────────────────────────────────────────────────────────────────────
#  File walking
# ───────────────────────────────────────────────────────────────────────
def _iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in p.parts):
            continue
        if p.suffix in suffixes:
            out.append(p)
    return out


def _py_files(root: Path) -> list[Path]:
    return _iter_files(root, (".py",))


def _rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _parse(p: Path) -> ast.Module | None:
    try:
        return ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


# ───────────────────────────────────────────────────────────────────────
#  Dependencies + languages + frameworks
# ───────────────────────────────────────────────────────────────────────
_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def detect_dependencies(root: Path) -> list[str]:
    """Union of declared deps across requirements*.txt, pyproject.toml, setup.py."""
    deps: set[str] = set()

    for req in root.rglob("requirements*.txt"):
        if any(part in _SKIP_DIRS for part in req.parts):
            continue
        for line in req.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            m = _REQ_LINE.match(line)
            if m:
                deps.add(m.group(1).lower())

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(errors="ignore"))
            proj = data.get("project", {})
            for d in proj.get("dependencies", []) or []:
                m = _REQ_LINE.match(str(d))
                if m:
                    deps.add(m.group(1).lower())
            # poetry
            poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            for name in poetry:
                if name.lower() != "python":
                    deps.add(name.lower())
        except Exception:
            pass

    setup = root / "setup.py"
    if setup.is_file():
        txt = setup.read_text(errors="ignore")
        for m in re.finditer(r"['\"]([A-Za-z0-9_.\-]+)\s*(?:[<>=!~].*)?['\"]", txt):
            name = m.group(1).lower()
            if name in _FRAMEWORK_MAP:
                deps.add(name)

    return sorted(deps)


def detect_languages(root: Path) -> list[str]:
    ext_lang = {
        ".py": "Python", ".ipynb": "Jupyter", ".js": "JavaScript", ".ts": "TypeScript",
        ".java": "Java", ".rb": "Ruby", ".go": "Go", ".rs": "Rust", ".scala": "Scala",
    }
    counts: dict[str, int] = {}
    for p in root.rglob("*"):
        if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
            continue
        lang = ext_lang.get(p.suffix)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return [lang for lang, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def detect_frameworks(deps: list[str], root: Path) -> list[str]:
    """Map declared deps → human framework labels; fall back to import scanning."""
    labels: list[str] = []
    seen: set[str] = set()
    for d in deps:
        hit = _FRAMEWORK_MAP.get(d)
        if hit and hit[0] not in seen:
            labels.append(hit[0])
            seen.add(hit[0])

    # If deps were sparse, scan imports directly.
    if not labels:
        imported: set[str] = set()
        for p in _py_files(root)[:400]:
            mod = _parse(p)
            if not mod:
                continue
            for node in ast.walk(mod):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        imported.add(a.name.split(".")[0].lower())
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0].lower())
        for name in imported:
            hit = _FRAMEWORK_MAP.get(name)
            if hit and hit[0] not in seen:
                labels.append(hit[0])
                seen.add(hit[0])
    return labels


# ───────────────────────────────────────────────────────────────────────
#  Models
# ───────────────────────────────────────────────────────────────────────
def _base_names(node: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            names.append(b.id)
        elif isinstance(b, ast.Attribute):
            names.append(b.attr)
    return names


def detect_models(root: Path, frameworks: list[str]) -> list:
    """Find model classes (nn.Module subclasses, keras Models) + estimator usage."""
    from airen.onboarding.manifest import DetectedModel

    fw_lower = {f.lower() for f in frameworks}
    dl_fw = (
        "PyTorch" if any("pytorch" in f or "torch" in f for f in fw_lower) else
        "TensorFlow" if any("tensorflow" in f or "keras" in f for f in fw_lower) else
        None
    )
    found: dict[str, DetectedModel] = {}

    for p in _py_files(root):
        mod = _parse(p)
        if not mod:
            continue
        rel = _rel(root, p)
        for node in ast.walk(mod):
            # nn.Module / keras Model subclasses
            if isinstance(node, ast.ClassDef):
                bases = _base_names(node)
                if any(b in ("Module", "LightningModule") for b in bases):
                    found.setdefault(node.name, DetectedModel(
                        name=node.name, kind="deep-learning", framework=dl_fw or "PyTorch",
                        file=rel, line=node.lineno, confidence=0.9,
                    ))
                elif any(b in ("Model",) for b in bases) and dl_fw:
                    found.setdefault(node.name, DetectedModel(
                        name=node.name, kind="deep-learning", framework=dl_fw,
                        file=rel, line=node.lineno, confidence=0.7,
                    ))
            # estimator instantiation: X = RandomForestClassifier(...)
            if isinstance(node, ast.Call):
                fn = node.func
                cname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                if cname in _ESTIMATOR_NAMES:
                    kind = _ESTIMATOR_NAMES[cname]
                    fw = "Keras" if cname == "Sequential" else "scikit-learn/boosting"
                    found.setdefault(cname, DetectedModel(
                        name=cname, kind=kind, framework=fw,
                        file=rel, line=getattr(node, "lineno", None), confidence=0.8,
                    ))
    # rank: higher confidence first, then by name
    return sorted(found.values(), key=lambda m: (-m.confidence, m.name))


# ───────────────────────────────────────────────────────────────────────
#  Serving APIs (FastAPI / Flask)
# ───────────────────────────────────────────────────────────────────────
_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def detect_apis(root: Path) -> list:
    from airen.onboarding.manifest import DetectedAPI

    apis: list[DetectedAPI] = []
    for p in _py_files(root):
        mod = _parse(p)
        if not mod:
            continue
        rel = _rel(root, p)
        for node in ast.walk(mod):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                api = _api_from_decorator(dec, node, rel)
                if api:
                    apis.append(api)
    return apis


def _api_from_decorator(dec: ast.expr, fn, rel: str):
    from airen.onboarding.manifest import DetectedAPI

    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    attr = dec.func.attr.lower()
    method: str | None = None
    if attr in _HTTP_METHODS:                  # @app.post("/predict") / @router.get(...)
        method = attr.upper()
    elif attr == "route":                       # Flask @app.route("/x", methods=["POST"])
        method = "GET"
        for kw in dec.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                vals = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                if vals:
                    method = str(vals[0]).upper()
    if method is None:
        return None
    path = "?"
    if dec.args and isinstance(dec.args[0], ast.Constant):
        path = str(dec.args[0].value)

    # request model: first arg with a Name/Attribute annotation that isn't a primitive
    req_model = None
    for a in fn.args.args:
        if a.annotation is not None and a.arg not in ("self", "request"):
            ann = _annotation_name(a.annotation)
            if ann and ann not in ("int", "str", "float", "bool", "dict", "list", "Request"):
                req_model = ann
                break
    resp_model = _annotation_name(fn.returns) if fn.returns is not None else None
    # FastAPI response_model= kwarg overrides
    for kw in dec.keywords:
        if kw.arg == "response_model":
            resp_model = _annotation_name(kw.value) or resp_model

    return DetectedAPI(
        method=method, path=path, handler=fn.name, file=rel,
        line=fn.lineno, request_model=req_model, response_model=resp_model,
    )


def _annotation_name(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.value)
    if isinstance(node, ast.Constant):
        return str(node.value)
    return None


# ───────────────────────────────────────────────────────────────────────
#  Observability + tracking
# ───────────────────────────────────────────────────────────────────────
def detect_observability(root: Path):
    from airen.onboarding.manifest import Observability

    obs = Observability()
    files: set[str] = set()
    proj_pat = re.compile(r"project_name\s*=\s*['\"]([^'\"]+)['\"]")
    for p in _py_files(root):
        txt = p.read_text(errors="ignore")
        low = txt.lower()
        hit = False
        if "phoenix" in low or "px.register" in low or "px.launch_app" in low:
            obs.uses_phoenix = True
            hit = True
            if obs.phoenix_project is None:
                m = proj_pat.search(txt)
                if m:
                    obs.phoenix_project = m.group(1)
        if "openinference" in low:
            obs.uses_openinference = True
            hit = True
        if "opentelemetry" in low or "from otel" in low:
            obs.uses_otel = True
            hit = True
        if hit:
            files.add(_rel(root, p))
    obs.files = sorted(files)
    return obs


def detect_tracking(root: Path):
    from airen.onboarding.manifest import Tracking

    trk = Tracking()
    files: set[str] = set()
    names: set[str] = set()
    exp_pat = re.compile(r"(?:set_experiment|create_experiment)\s*\(\s*['\"]([^'\"]+)['\"]")
    for p in _py_files(root):
        txt = p.read_text(errors="ignore")
        if "mlflow" in txt.lower():
            trk.uses_mlflow = True
            files.add(_rel(root, p))
            for m in exp_pat.finditer(txt):
                names.add(m.group(1))
    trk.experiment_names = sorted(names)
    trk.files = sorted(files)
    return trk


# ───────────────────────────────────────────────────────────────────────
#  Training-code presence — does this repo TRAIN models, or only SERVE them?
#  When a service only serves (training lives in a separate repo), onboarding
#  asks the user for the training repo so Airen can check train↔serve parity.
# ───────────────────────────────────────────────────────────────────────
_SKIP_PATH = re.compile(r"(^|/)(tests?|conftest)(/|\.|_)", re.I)

# A file/dir that looks like a TRAINING ENTRYPOINT (where models are produced).
_TRAIN_LOC = re.compile(r"(?:^|/)(?:[^/]*train[^/]*|experiments?|finetune|model_training)(?:/|\.py$)", re.I)

# Persisting a MODEL = training, wherever it appears (serving LOADS, never saves).
_PERSIST_SIGNALS = [
    (re.compile(r"\btorch\.save\s*\("), "saves a trained torch model (torch.save)"),
    (re.compile(r"mlflow\.(?:log_model|autolog)"), "logs a model to mlflow"),
    (re.compile(r"\bjoblib\.dump\s*\("), "persists a model (joblib.dump)"),
    (re.compile(r"\.save_model\s*\("), "saves a model (.save_model)"),
]

# In-training-loop signals — counted ONLY inside a training-named location, because
# serving code legitimately fits/splits per-request (we saw that in predict/processing).
_TRAIN_LOOP_SIGNALS = [
    (re.compile(r"\.backward\s*\("), "backprop (loss backward)"),
    (re.compile(r"\boptimizer\.step\s*\("), "optimizer step"),
    (re.compile(r"\btrain_test_split\s*\("), "train/test split"),
    (re.compile(r"\.fit\s*\("), "estimator/model fit"),
    (re.compile(r"mlflow\.(?:log_|start_run)"), "mlflow training run"),
    (re.compile(r"\bGridSearchCV\b|\bRandomizedSearchCV\b"), "hyperparameter search"),
]


def detect_training_presence(root: Path, max_files: int = 1000) -> tuple[bool, list[str]]:
    """Does this repo TRAIN the models, or only SERVE them? Returns
    (has_training_code, evidence[file:line]). True if it persists a model anywhere,
    or runs a training loop inside a training-named file/dir. A serving-only repo
    (predict/ that loads models and may fit per-request scalers) reads as False —
    so onboarding asks for the separate training repo. Biased to under-detect."""
    evidence: list[str] = []
    for p in _py_files(root)[:max_files]:
        rel = _rel(root, p)
        if _SKIP_PATH.search(rel):
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue

        def _hit(pat, label):
            m = pat.search(text)
            if m:
                evidence.append(f"{label} @ {rel}:{text[: m.start()].count(chr(10)) + 1}")
                return True
            return False

        # model persistence anywhere → training
        if any(_hit(pat, label) for pat, label in _PERSIST_SIGNALS):
            pass
        # training-loop signals only inside a training-named location
        elif _TRAIN_LOC.search(rel):
            for pat, label in _TRAIN_LOOP_SIGNALS:
                if _hit(pat, label):
                    break
        if len(evidence) >= 8:
            break
    return (len(evidence) > 0), evidence


# ───────────────────────────────────────────────────────────────────────
#  Feature constants → attributes_of_interest candidates
# ───────────────────────────────────────────────────────────────────────
def detect_feature_constants(root: Path, max_results: int = 12) -> list[str]:
    """Module-level numeric assignments whose name looks like a tunable knob."""
    freq: dict[str, int] = {}
    for p in _py_files(root):
        mod = _parse(p)
        if not mod:
            continue
        for node in mod.body:  # module-level only
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, (int, float)) and not isinstance(node.value.value, bool):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        name = tgt.id
                        low = name.lower()
                        if any(stem in low for stem in _FEATURE_STEMS) and not low.startswith("_"):
                            freq[name] = freq.get(name, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:max_results]]


# ───────────────────────────────────────────────────────────────────────
#  Serving topology — how the inference layer emits predictions
# ───────────────────────────────────────────────────────────────────────
_KAFKA_LIBS = {"kafka", "kafka-python", "confluent-kafka", "confluent_kafka", "aiokafka", "faust", "pykafka"}
_KAFKA_CONSUMER_CTORS = {"KafkaConsumer", "AIOKafkaConsumer", "Consumer"}
_KAFKA_PRODUCER_CTORS = {"KafkaProducer", "AIOKafkaProducer", "Producer"}
_SCHED_LIBS = {"airflow": "airflow", "prefect": "prefect", "dagster": "dagster", "luigi": "luigi", "kedro": "kedro"}


def _first_str_arg(call: ast.Call) -> str | None:
    for a in call.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
    for kw in call.keywords:
        if kw.arg in ("topic", "topics") and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def detect_serving_topology(root: Path, deps: list[str], apis: list):
    """Classify the inference I/O channel: sync-api | async-kafka | batch."""
    from airen.onboarding.manifest import ServingTopology

    dep_set = set(deps)
    libs = sorted(_KAFKA_LIBS & dep_set)
    scheduler = next((label for name, label in _SCHED_LIBS.items() if name in dep_set), None)

    consumer = producer = False
    topics: set[str] = set()

    for p in _py_files(root):
        mod = _parse(p)
        if not mod:
            continue
        for node in ast.walk(mod):
            # imports (catches libs not declared in deps)
            if isinstance(node, ast.Import):
                for a in node.names:
                    base = a.name.split(".")[0]
                    if base in _KAFKA_LIBS:
                        libs = sorted(set(libs) | {base})
                    if base in _SCHED_LIBS and not scheduler:
                        scheduler = _SCHED_LIBS[base]
            elif isinstance(node, ast.ImportFrom) and node.module:
                base = node.module.split(".")[0]
                if base in _KAFKA_LIBS:
                    libs = sorted(set(libs) | {base})
                if base in _SCHED_LIBS and not scheduler:
                    scheduler = _SCHED_LIBS[base]
            # consumer/producer construction + send/produce/subscribe topics
            elif isinstance(node, ast.Call):
                fn = node.func
                cname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                if cname in _KAFKA_CONSUMER_CTORS:
                    consumer = True
                    t = _first_str_arg(node)
                    if t:
                        topics.add(t)
                elif cname in _KAFKA_PRODUCER_CTORS:
                    producer = True
                elif cname in ("send", "produce", "send_and_wait"):
                    producer = True
                    t = _first_str_arg(node)
                    if t:
                        topics.add(t)
                elif cname == "subscribe":
                    consumer = True
                    for a in node.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            topics.add(a.value)
                        elif isinstance(a, (ast.List, ast.Tuple)):
                            for e in a.elts:
                                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                                    topics.add(e.value)

    # also treat DAG files as a batch signal
    if not scheduler:
        for p in root.rglob("*"):
            if p.is_file() and (p.name.endswith("_dag.py") or "dags" in p.parts) \
                    and not any(part in _SKIP_DIRS for part in p.parts):
                scheduler = "airflow"
                break

    has_kafka = bool(libs) or consumer or producer
    if (consumer and producer) or (has_kafka and not apis):
        mode = "async-kafka"
    elif apis:
        mode = "sync-api"
    elif scheduler:
        mode = "batch"
    elif has_kafka:
        mode = "async-kafka"
    else:
        mode = "unknown"

    endpoints = [f"{a.method} {a.path}" for a in apis]
    return ServingTopology(
        mode=mode, endpoints=endpoints, kafka_topics=sorted(topics),
        kafka_consumer=consumer, kafka_producer=producer,
        scheduler=scheduler, libraries=libs,
    )


# ───────────────────────────────────────────────────────────────────────
#  Observation schema — which span attrs Sentinel should read
# ───────────────────────────────────────────────────────────────────────
_SPAN_ATTR = re.compile(r"['\"]((?:eval|input)\.[A-Za-z0-9_.]+)['\"]")
_ERROR_HINTS = ("error", "mae", "rmse", "mape", "loss", "abs", "residual", "delta", "diff")
_SCORE_HINTS = ("correct", "accuracy", "acc", "f1", "precision", "recall", "auc", "hit", "ndcg")


def detect_observation_schema(root: Path, models: list):
    """Find the eval/input span attributes the app emits, so Sentinel can read
    the right error attribute + segmentation dims for THIS app (not TL-ETA's)."""
    from airen.onboarding.manifest import ObservationSchema

    eval_attrs: set[str] = set()
    input_attrs: set[str] = set()
    for p in _py_files(root):
        txt = p.read_text(errors="ignore")
        if "eval." not in txt and "input." not in txt:
            continue
        for m in _SPAN_ATTR.finditer(txt):
            a = m.group(1)
            (eval_attrs if a.startswith("eval.") else input_attrs).add(a)

    # error attribute: prefer an eval.* name that looks like an error/score metric
    error_attr = None
    ranked_eval = sorted(eval_attrs)
    for a in ranked_eval:
        low = a.lower()
        if any(h in low for h in _ERROR_HINTS + _SCORE_HINTS):
            error_attr = a
            break
    if error_attr is None and ranked_eval:
        error_attr = ranked_eval[0]

    # problem type: classification if a model/estimator/metric says so
    kinds = " ".join((m.kind or "") + " " + (m.name or "") for m in models).lower()
    has_score_metric = any(h in (error_attr or "").lower() for h in _SCORE_HINTS)
    if "classif" in kinds or has_score_metric:
        problem_type = "classification"
    else:
        problem_type = "regression"

    return ObservationSchema(
        problem_type=problem_type,
        error_attribute=error_attr,
        segment_candidates=sorted(input_attrs)[:6],
        eval_attributes=ranked_eval,
    )


# ───────────────────────────────────────────────────────────────────────
#  Application type classification
# ───────────────────────────────────────────────────────────────────────
def classify_application(apis: list, frameworks: list[str], root: Path) -> str:
    if apis:
        return "model-serving-api"
    fw = {f.lower() for f in frameworks}
    has_setup = (root / "setup.py").is_file() or (root / "pyproject.toml").is_file()
    if any(k in fw for k in ("pytorch", "tensorflow", "scikit-learn", "xgboost", "lightgbm")):
        return "batch-pipeline"
    if has_setup and not apis:
        return "library"
    return "unknown"
