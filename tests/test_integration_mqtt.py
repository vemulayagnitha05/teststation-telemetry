"""Integration test against a real Mosquitto broker.

Skipped unless MQTT_HOST is set, e.g.:
    docker compose up -d --wait mosquitto
    MQTT_HOST=localhost python -m unittest tests.test_integration_mqtt -v
"""
import os
import tempfile
import time
import unittest

from telemetry.ingestor import Ingestor
from telemetry.simulator import FaultSpec, Simulator
from telemetry.store import Store
from telemetry.transport import PahoTransport

HOST = os.environ.get("MQTT_HOST")
PORT = int(os.environ.get("MQTT_PORT", "1883"))


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(HOST, "set MQTT_HOST to run against a real broker")
class BrokerIntegrationTests(unittest.TestCase):
    def test_messages_flow_end_to_end(self):
        uid = f"{os.getpid()}-{int(time.time())}"
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "it.db"))
            sub = PahoTransport(HOST, PORT, client_id=f"it-sub-{uid}")
            pub = PahoTransport(HOST, PORT, client_id=f"it-pub-{uid}")
            try:
                ingestor = Ingestor(sub, store)
                ingestor.start()
                sim = Simulator(n_stations=2, n_ticks=15,
                                faults=[FaultSpec("malformed", "st-01", 3, 5)])
                messages = [m for _, msgs in sim.run() for m in msgs]
                for topic, payload in messages:
                    pub.publish(topic, payload)
                pub.publish(*messages[0])  # simulate a redelivery of the first message
                pub.flush()
                expected = len(messages) + 1
                self.assertTrue(wait_for(lambda: ingestor.stats.received >= expected),
                                f"only {ingestor.stats.received}/{expected} received")
                self.assertEqual(ingestor.stats.rejected, 2)
                self.assertEqual(ingestor.stats.duplicates, 1)
                counts = store.counts()
                self.assertEqual(counts["results"], 2 * 15 * len(sim.checks))
                self.assertEqual(counts["heartbeats"], 2 * 15)
            finally:
                pub.close()
                sub.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
