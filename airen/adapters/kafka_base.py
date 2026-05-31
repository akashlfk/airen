"""Abstract Kafka adapter — the contract every implementation honors.

Why a Protocol instead of an ABC: we want duck-typing for tests, and a Protocol
lets MockKafkaAdapter not formally inherit while still satisfying the type.

What a "message" is, in our shape: a plain dict. The adapter does the decoding
(JSON / Avro / whatever the real broker uses); upstream code never sees raw bytes.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol


class KafkaAdapter(Protocol):
    """A Kafka consumer that yields decoded prediction messages.

    Implementations:
      - MockKafkaAdapter (airen.adapters.kafka_mock) — synthesizes messages
      - RealKafkaAdapter (airen.adapters.kafka_real) — wraps confluent-kafka-python
    """

    def consume(self, max_messages: int | None = None) -> Iterator[dict[str, Any]]:
        """Yield decoded messages from the topic.

        Args:
            max_messages: stop after this many. None = run forever.

        Yields:
            dict — one decoded prediction event.
        """
        ...

    def close(self) -> None:
        """Release any underlying resources (broker connection, consumer group)."""
        ...
