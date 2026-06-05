"""Run the Kafka tap.

REAL mode is PEEK-ONLY — see memory/kafka_peek_only.md. Each run reads the
LATEST N messages from the topic for validation or sniff-testing, then exits.
No consumer group ownership, no offset commits, no continuous streaming.
Historical / analytical queries go through the Redshift adapter.

Modes:
  --mode mock   (default)   — synthesizes messages locally, no broker needed
  --mode real               — peek at latest N from real Kafka (VPN required)

Examples:
  # Mock — 50 synthetic messages for testing the pipeline
  python -m airen.run_kafka_tap --mode mock --max 50

  # Real PEEK — last 10 messages from FK Kafka (default)
  python -m airen.run_kafka_tap --mode real

  # Real PEEK — last 50 messages
  python -m airen.run_kafka_tap --mode real --max 50

  # Real PEEK on a LOOP — re-tap the latest 10k every 5 min to keep Phoenix
  # fresh (each pass is still a bounded peek; Ctrl+C to stop). Good for a PoC.
  python -m airen.run_kafka_tap --mode real --max 10000 --loop 300

NOTE: --rate / streaming flags only apply to --mode mock. Each pass (even in
loop mode) is a bounded peek of the latest N — no consumer group, no offset
commits. --loop just repeats that peek on an interval; it is NOT a streaming
consumer.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# The tap writes spans to the MONITORED SERVICE's Phoenix project (not Airen's
# own 'airen-dev' project), so the tracer/register happens in main() once we
# know the project (from --service or PHOENIX_PROJECT_NAME_TAP).
from phoenix.otel import register  # noqa: E402

from airen.adapters.kafka_base import KafkaAdapter  # noqa: E402
from airen.adapters.kafka_mock import MockKafkaAdapter  # noqa: E402
from airen.ingestion.kafka_tap import KafkaTap  # noqa: E402


def _build_adapter(mode: str, args: argparse.Namespace) -> KafkaAdapter:
    if mode == "mock":
        return MockKafkaAdapter(
            rate_per_second=args.rate,
            bugged_fraction=args.bugged_fraction,
            fail_fraction=args.fail_fraction,
            seed=args.seed,
        )
    if mode == "real":
        from airen.adapters.kafka_real import RealKafkaAdapter

        # Resolve topic: CLI --topic > --source flag > KAFKA_TOPIC > KAFKA_OUTPUT_TOPIC
        explicit = getattr(args, "topic", None)
        source = getattr(args, "source", "output")
        if not explicit:
            explicit = os.environ.get(f"KAFKA_{source.upper()}_TOPIC")
        kc = getattr(args, "_kafka_cfg", None)   # yaml broker/protocol/mechanism (creds stay in .env)
        return RealKafkaAdapter(
            topic=explicit,
            bootstrap_servers=kc.bootstrap_servers if kc else None,
            security_protocol=kc.security_protocol if kc else None,
            sasl_mechanism=kc.sasl_mechanism if kc else None,
        )
    raise ValueError(f"unknown mode: {mode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "real"], default=None,
                        help="default: real when --service is given, else mock")
    parser.add_argument("--max", type=int, default=None, help="stop after N messages (real default: 10)")
    parser.add_argument(
        "--source",
        choices=["output", "input"],
        default="output",
        help="real: peek the OUTPUT topic (predictions, default) or INPUT topic (requests)",
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="real: explicit topic name (overrides --source and env vars)",
    )
    parser.add_argument("--rate", type=float, default=10.0, help="mock: msgs/sec")
    parser.add_argument("--bugged-fraction", type=float, default=0.20, dest="bugged_fraction")
    parser.add_argument("--fail-fraction", type=float, default=0.01, dest="fail_fraction")
    parser.add_argument("--seed", type=int, default=None, help="mock: deterministic seed")
    parser.add_argument(
        "--service", default=None,
        help="onboarded service name — loads its airen.yaml for the Phoenix project, "
             "Kafka output_topic, and tap.field_map (config-driven mapping).",
    )
    parser.add_argument(
        "--loop", type=int, default=0, metavar="SECONDS",
        help="re-tap the latest N every SECONDS (0 = one-shot). Each pass is still "
             "a bounded peek; keeps Phoenix's recent window fresh. Ctrl+C to stop.",
    )
    args = parser.parse_args()

    # Naming a real service implies you want REAL data — default to real mode so
    # you don't silently get synthetic messages.
    if args.mode is None:
        args.mode = "real" if args.service else "mock"

    # Resolve the Phoenix project + (optional) config-driven field map + kafka conn.
    project = os.environ.get("PHOENIX_PROJECT_NAME_TAP", "airen-tap")
    field_map = None
    kafka_cfg = None
    if args.service:
        from airen.config import load_service_config

        cfg = load_service_config(args.service)
        project = cfg.phoenix.project_name
        field_map = cfg.tap.field_map
        kafka_cfg = cfg.kafka  # bootstrap/protocol/mechanism from the yaml
        # Default the real-mode topic to the service's configured output topic.
        if cfg.kafka and cfg.kafka.output_topic and not args.topic:
            args.topic = cfg.kafka.output_topic
        print(f"Using service '{args.service}': mode={args.mode}, project={project}, "
              f"topic={args.topic or '(env)'}, field_map={'yes' if field_map else 'default'}")
    args._kafka_cfg = kafka_cfg  # consumed by _build_adapter (real mode)

    # Register the tracer against the SERVICE's project (not airen-dev).
    tracer_provider = register(project_name=project, auto_instrument=False)
    tracer = tracer_provider.get_tracer("airen.ingestion.kafka_tap")

    if args.mode == "real":
        if args.max is None:
            args.max = 10  # safe peek default
        resolved_topic = (
            args.topic
            or os.environ.get(f"KAFKA_{args.source.upper()}_TOPIC")
            or os.environ.get("KAFKA_TOPIC", "?")
        )
        print(f"Kafka PEEK starting — latest {args.max} from {resolved_topic} (source={args.source})")
        print(f"  (ephemeral group.id, no offset commits, no subscribe — see memory/kafka_peek_only.md)")
    else:
        print(f"Kafka MOCK starting — project={project}, rate={args.rate} msg/s, bugged={args.bugged_fraction:.0%}")

    def _one_pass() -> None:
        # Fresh adapter each pass — a new ephemeral peek (no lingering connection).
        adapter = _build_adapter(args.mode, args)
        tap = KafkaTap(adapter=adapter, tracer=tracer, field_map=field_map)
        tap.run(max_messages=args.max)
        tracer_provider.force_flush()

    if args.loop and args.loop > 0:
        print(f"🔁 LOOP mode — re-tapping every {args.loop}s. Ctrl+C to stop.\n")
        n = 0
        try:
            while True:
                n += 1
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"\n── pass {n} · {ts} UTC ──")
                _one_pass()
                print(f"💤 sleeping {args.loop}s…")
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print(f"\n✓ Stopped after {n} pass(es).")
    else:
        _one_pass()


if __name__ == "__main__":
    main()
