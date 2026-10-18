import types
import unittest

from telemetry.transport import InMemoryBroker, PahoTransport, topic_matches


class TopicMatchTests(unittest.TestCase):
    def test_patterns(self):
        cases = [
            ("teststation/+/results", "teststation/st-01/results", True),
            ("teststation/+/results", "teststation/st-01/heartbeat", False),
            ("teststation/+/results", "teststation/st-01/x/results", False),
            ("teststation/#", "teststation/st-01/results", True),
            ("teststation/#", "other/st-01", False),
            ("a/b", "a/b", True),
            ("a/b/c", "a/b", False),
            ("a/b", "a/b/c", False),
        ]
        for pattern, topic, expected in cases:
            with self.subTest(pattern=pattern, topic=topic):
                self.assertEqual(topic_matches(pattern, topic), expected)


class InMemoryBrokerTests(unittest.TestCase):
    def test_delivers_only_to_matching_subscribers(self):
        broker = InMemoryBroker()
        got_results, got_all = [], []
        broker.subscribe("teststation/+/results", lambda t, p: got_results.append((t, p)))
        broker.subscribe("teststation/#", lambda t, p: got_all.append(t))
        broker.publish("teststation/st-01/results", b"x")
        broker.publish("teststation/st-01/heartbeat", b"y")
        self.assertEqual(got_results, [("teststation/st-01/results", b"x")])
        self.assertEqual(len(got_all), 2)
        self.assertEqual(broker.published, 2)


class FakeInfo:
    def wait_for_publish(self, timeout=None):
        return True


class FakeClient:
    """Stands in for paho.mqtt.client.Client to exercise PahoTransport's own logic."""

    def __init__(self, api_version, client_id=""):
        self.api_version, self.client_id = api_version, client_id
        self.subscribed, self.published = [], []
        self._mid = 0

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect(self, host, port, keepalive):
        self.addr = (host, port)

    def loop_start(self):
        self.on_connect(self, None, {}, 0, None)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, pattern, qos):
        self._mid += 1
        self.subscribed.append((pattern, qos))
        self.on_subscribe(self, None, self._mid, [qos], None)  # SUBACK arrives immediately
        return (0, self._mid)

    def publish(self, topic, payload, qos):
        self.published.append((topic, payload, qos))
        return FakeInfo()


class PahoTransportTests(unittest.TestCase):
    def make(self):
        mod = types.SimpleNamespace(Client=FakeClient,
                                    CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))
        return PahoTransport("broker", 1883, client_id="t", mqtt_module=mod)

    def test_subscribe_dispatch_and_publish(self):
        transport = self.make()
        seen = []
        transport.subscribe("teststation/+/results", lambda t, p: seen.append((t, p)))
        msg = types.SimpleNamespace(topic="teststation/st-01/results", payload=bytearray(b"hi"))
        transport._on_message(None, None, msg)
        other = types.SimpleNamespace(topic="teststation/st-01/heartbeat", payload=b"no")
        transport._on_message(None, None, other)
        self.assertEqual(seen, [("teststation/st-01/results", b"hi")])
        transport.publish("a/b", b"z")
        self.assertEqual(transport._client.published, [("a/b", b"z", 1)])
        transport.flush()
        transport.close()

    def test_failing_handler_does_not_propagate(self):
        transport = self.make()

        def boom(topic, payload):
            raise RuntimeError("bad handler")

        transport.subscribe("a/#", boom)
        msg = types.SimpleNamespace(topic="a/b", payload=b"")
        transport._on_message(None, None, msg)  # must not raise

    def test_resubscribes_after_reconnect(self):
        transport = self.make()
        transport.subscribe("a/#", lambda t, p: None)
        before = len(transport._client.subscribed)
        transport._on_connect(transport._client, None, {}, 0, None)
        self.assertEqual(len(transport._client.subscribed), before + 1)


if __name__ == "__main__":
    unittest.main()
