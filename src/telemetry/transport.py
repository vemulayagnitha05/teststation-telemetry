"""Transport abstraction: an in-memory broker for tests/demo and a paho-mqtt client."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Protocol

log = logging.getLogger(__name__)

Handler = Callable[[str, bytes], None]


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT topic filter matching with '+' (one level) and '#' (rest) wildcards."""
    p_parts = pattern.split("/")
    t_parts = topic.split("/")
    for i, part in enumerate(p_parts):
        if part == "#":
            return True
        if i >= len(t_parts):
            return False
        if part != "+" and part != t_parts[i]:
            return False
    return len(p_parts) == len(t_parts)


class Transport(Protocol):
    """What the ingestor and simulator need from a message transport."""

    def publish(self, topic: str, payload: bytes) -> None:
        raise NotImplementedError

    def subscribe(self, pattern: str, handler: Handler) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class InMemoryBroker:
    """Synchronous broker with MQTT wildcard semantics. Used by unit tests and `demo`."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self.published = 0

    def publish(self, topic: str, payload: bytes) -> None:
        self.published += 1
        for pattern, handler in list(self._subs):
            if topic_matches(pattern, topic):
                handler(topic, payload)

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))

    def flush(self, timeout: float = 0.0) -> None:
        return None

    def close(self) -> None:
        self._subs.clear()


class PahoTransport:
    """paho-mqtt (>=2.0) client with connect-retry, resubscribe on reconnect, QoS 1.

    Subscription patterns registered on one client must not overlap, otherwise the
    broker delivers a message once per matching filter and handlers would run twice.
    """

    def __init__(self, host: str, port: int = 1883, client_id: str = "", qos: int = 1,
                 connect_timeout: float = 30.0, mqtt_module=None) -> None:
        if mqtt_module is None:
            import paho.mqtt.client as mqtt_module
        self._qos = qos
        self._subs: list[tuple[str, Handler]] = []
        self._connected = threading.Event()
        self._cond = threading.Condition()
        self._acked: set[int] = set()
        self._last_info = None
        self._client = mqtt_module.Client(mqtt_module.CallbackAPIVersion.VERSION2,
                                          client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe
        self._client.reconnect_delay_set(min_delay=1, max_delay=10)
        deadline = time.monotonic() + connect_timeout
        while True:
            try:
                self._client.connect(host, port, keepalive=30)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ConnectionError(f"cannot reach MQTT broker {host}:{port}") from exc
                log.info("broker %s:%s not reachable yet, retrying", host, port)
                time.sleep(1.0)
        self._client.loop_start()
        if not self._connected.wait(connect_timeout):
            raise ConnectionError(f"no CONNACK from {host}:{port}")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.error("MQTT connect refused: %s", reason_code)
            return
        for pattern, _ in self._subs:  # re-subscribe after a reconnect
            client.subscribe(pattern, self._qos)
        self._connected.set()

    def _on_subscribe(self, client, userdata, mid, reason_codes=None, properties=None) -> None:
        with self._cond:
            self._acked.add(mid)
            self._cond.notify_all()

    def _on_message(self, client, userdata, msg) -> None:
        for pattern, handler in self._subs:
            if topic_matches(pattern, msg.topic):
                try:
                    handler(msg.topic, bytes(msg.payload))
                except Exception:  # a bad message must never kill the network loop
                    log.exception("handler failed for topic %s", msg.topic)

    def publish(self, topic: str, payload: bytes) -> None:
        self._last_info = self._client.publish(topic, payload, qos=self._qos)

    def subscribe(self, pattern: str, handler: Handler, timeout: float = 10.0) -> None:
        self._subs.append((pattern, handler))
        _, mid = self._client.subscribe(pattern, self._qos)
        with self._cond:
            if not self._cond.wait_for(lambda: mid in self._acked, timeout):
                raise TimeoutError(f"no SUBACK for {pattern!r}")

    def flush(self, timeout: float = 10.0) -> None:
        """Block until the most recent publish has been acknowledged by the broker."""
        if self._last_info is not None:
            self._last_info.wait_for_publish(timeout)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def make_transport(host: Optional[str], port: int = 1883, client_id: str = "") -> PahoTransport:
    if not host:
        raise ValueError("an MQTT host is required (--host or MQTT_HOST)")
    return PahoTransport(host, port, client_id=client_id)
