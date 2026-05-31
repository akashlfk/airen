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

NOTE: --rate / streaming flags only apply to --mode mock. Real mode is
always a bounded one-shot peek; do not try to use it for continuous monitoring.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from airen.instrumentation import setup_tracing

PROJECT_NAME = os.environ.get("PHOENIX_PROJECT_NAME_TAP", "tl-eta-prediction")
tracer_provider = setup_tracing()
tracer = tracer_provider.get_tracer("mlre.ingestion.kafka_tap")

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
        return RealKafkaAdapter(topic=explicit)
    raise ValueError(f"unknown mode: {mode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
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
    args = parser.parse_args()

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
        print(f"Kafka MOCK starting — project={PROJECT_NAME}, rate={args.rate} msg/s, bugged={args.bugged_fraction:.0%}")
    adapter = _build_adapter(args.mode, args)
    tap = KafkaTap(adapter=adapter, tracer=tracer)
    tap.run(max_messages=args.max)

    tracer_provider.force_flush()


if __name__ == "__main__":
    main()
