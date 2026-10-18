"""Subscribes to telemetry topics, validates messages and stores them."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from .schema import (HEARTBEAT_PATTERN, RESULTS_PATTERN, ValidationError, parse_heartbeat,
                     parse_result)
from .store import Store
from .transport import Transport

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    received: int = 0
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class Ingestor:
    def __init__(self, transport: Transport, store: Store) -> None:
        self.transport = transport
        self.store = store
        self.stats = IngestStats()

    def start(self) -> None:
        self.transport.subscribe(RESULTS_PATTERN, self.handle_result)
        self.transport.subscribe(HEARTBEAT_PATTERN, self.handle_heartbeat)

    def handle_result(self, topic: str, payload: bytes) -> None:
        self.stats.received += 1
        try:
            result = parse_result(payload, topic)
        except ValidationError as exc:
            self._reject(topic, payload, exc)
            return
        self._count(self.store.insert_result(result))

    def handle_heartbeat(self, topic: str, payload: bytes) -> None:
        self.stats.received += 1
        try:
            beat = parse_heartbeat(payload, topic)
        except ValidationError as exc:
            self._reject(topic, payload, exc)
            return
        self._count(self.store.insert_heartbeat(beat))

    def _count(self, inserted: bool) -> None:
        if inserted:
            self.stats.stored += 1
        else:
            self.stats.duplicates += 1

    def _reject(self, topic: str, payload: bytes, exc: ValidationError) -> None:
        self.stats.rejected += 1
        log.warning("rejected message on %s: %s", topic, exc)
        self.store.record_rejected(topic, str(exc), payload)
