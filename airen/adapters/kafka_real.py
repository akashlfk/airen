"""Real Kafka adapter — PEEK-ONLY mode.

Design (see [[kafka-peek-only]]):
  We are NOT a regular consumer. We do not own a consumer group. We do not
  persist offsets. We do not subscribe.

  Each run is a one-shot peek at the LATEST N messages for validation /
  sniff-testing. Historical & analytical queries belong in the Redshift
  adapter, not here.

How it works:
  1. Connect with an ephemeral, random group.id (required by the SDK
     but never used for offset persistence).
  2. List partitions for the topic.
  3. For each partition, look up the high watermark and seek to
     `max(low, high - per_partition_count)`.
  4. assign() those (topic, partition, offset) tuples directly — bypassing
     subscribe()'s consumer-group rebalancing.
  5. poll() exactly N messages, decode, yield. Then close.

Auth via .env:
    KAFKA_BOOTSTRAP_SERVERS
    KAFKA_OUTPUT_TOPIC   (preferred default — predictions are what Sentinel monitors)
    KAFKA_INPUT_TOPIC    (request side — peek when validating upstream signals)
    KAFKA_TOPIC          (legacy override — used if set, else falls back to OUTPUT)
    KAFKA_SECURITY_PROTOCOL    e.g. SASL_SSL
    KAFKA_SASL_MECHANISM       e.g. PLAIN or SCRAM-SHA-256
    KAFKA_SASL_USERNAME
    KAFKA_SASL_PASSWORD
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Iterator


def _peek_group_id() -> str:
    """Random per-run group id — confluent-kafka requires the field but we never persist state."""
    return f"airen-peek-{uuid.uuid4().hex[:8]}"


class RealKafkaAdapter:
    """Peek-only Kafka client. Reads the last N messages and exits.

    NOT for continuous streaming. NOT a real consumer-group participant.
    Use Redshift adapter for historical / analytical queries.
    """

    def __init__(
        self,
        topic: str | None = None,
        partition_timeout_sec: float = 10.0,
        watermark_timeout_sec: float = 5.0,
        poll_timeout_sec: float = 5.0,
    ) -> None:
        try:
            from confluent_kafka import Consumer  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "confluent-kafka not installed. Install with:\n"
                "    conda run -n mlre pip install 'confluent-kafka>=2.5'"
            ) from e

        bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if not bootstrap:
            raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS not set in .env")
        # Topic resolution order:
        #   1. explicit `topic` arg
        #   2. KAFKA_TOPIC env (legacy single-topic config)
        #   3. KAFKA_OUTPUT_TOPIC env (predictions — Sentinel's primary signal)
        # For peeking the INPUT topic, pass topic=os.environ['KAFKA_INPUT_TOPIC'] explicitly.
        resolved = (
            (topic or "").strip()
            or os.environ.get("KAFKA_TOPIC", "").strip()
            or os.environ.get("KAFKA_OUTPUT_TOPIC", "").strip()
        )
        if not resolved:
            raise RuntimeError(
                "No Kafka topic configured. Set KAFKA_OUTPUT_TOPIC (or pass --topic explicitly)."
            )
        self.topic = resolved

        self._partition_timeout_sec = partition_timeout_sec
        self._watermark_timeout_sec = watermark_timeout_sec
        self._poll_timeout_sec = poll_timeout_sec

        config: dict[str, Any] = {
            "bootstrap.servers": bootstrap,
            # Ephemeral, NEVER persisted. Required by the SDK but meaningless to us.
            "group.id": _peek_group_id(),
            "enable.auto.commit": False,
            "session.timeout.ms": 10000,
        }
        if os.environ.get("KAFKA_SECURITY_PROTOCOL"):
            config["security.protocol"] = os.environ["KAFKA_SECURITY_PROTOCOL"]
        if os.environ.get("KAFKA_SASL_MECHANISM"):
            config["sasl.mechanism"] = os.environ["KAFKA_SASL_MECHANISM"]
        if os.environ.get("KAFKA_SASL_USERNAME"):
            config["sasl.username"] = os.environ["KAFKA_SASL_USERNAME"]
        if os.environ.get("KAFKA_SASL_PASSWORD"):
            config["sasl.password"] = os.environ["KAFKA_SASL_PASSWORD"]

        from confluent_kafka import Consumer

        self._consumer = Consumer(config)
        self._closed = False

    def consume(self, max_messages: int | None = None) -> Iterator[dict]:
        """Yield the LATEST `max_messages` messages from the topic, then stop.

        If max_messages is None, defaults to 10 (peek pattern — never unbounded).
        """
        from confluent_kafka import TopicPartition

        n = max_messages if max_messages is not None else 10
        if n <= 0:
            return

        # 1) Discover partitions
        metadata = self._consumer.list_topics(self.topic, timeout=self._partition_timeout_sec)
        if self.topic not in metadata.topics or metadata.topics[self.topic].error:
            self.close()
            raise RuntimeError(f"Topic {self.topic!r} not found or unreachable")
        partition_ids = sorted(metadata.topics[self.topic].partitions.keys())
        if not partition_ids:
            self.close()
            return

        # 2) For each partition, find high watermark, seek to (high - per_partition)
        per_partition = max(1, n // len(partition_ids) + 1)
        tps: list = []
        for pid in partition_ids:
            tp = TopicPartition(self.topic, pid)
            low, high = self._consumer.get_watermark_offsets(
                tp, timeout=self._watermark_timeout_sec, cached=False
            )
            if high <= low:
                continue  # empty partition
            start = max(low, high - per_partition)
            tps.append(TopicPartition(self.topic, pid, start))

        if not tps:
            print(f"  (topic {self.topic} has no recent messages)")
            self.close()
            return

        # 3) Assign directly (NOT subscribe — we are not a consumer group)
        self._consumer.assign(tps)

        # 4) Poll exactly n messages, then stop
        delivered = 0
        empty_polls = 0
        try:
            while delivered < n:
                msg = self._consumer.poll(timeout=self._poll_timeout_sec)
                if msg is None:
                    empty_polls += 1
                    if empty_polls >= 3:
                        # No more recent messages within poll window — done.
                        break
                    continue
                empty_polls = 0
                if msg.error():
                    continue
                decoded = self._decode(msg.value())
                if decoded is None:
                    continue
                # Stash partition/offset for diagnostics if the caller wants them
                decoded["_kafka_partition"] = msg.partition()
                decoded["_kafka_offset"] = msg.offset()
                delivered += 1
                yield decoded
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._consumer.close()
        except Exception:
            pass
        self._closed = True

    # ───── decoding ─────
    @staticmethod
    def _decode(raw: bytes | None) -> dict | None:
        """Decode the raw message bytes into our standard dict shape.

        Currently assumes JSON. When Akash pastes a real sample, we extend
        here to map FK's field names to our `mlre.*` Phoenix span shape.
        """
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
