import unittest

from telemetry.ingestor import Ingestor
from telemetry.schema import Heartbeat, heartbeat_topic, results_topic
from telemetry.store import Store
from telemetry.transport import InMemoryBroker

from .test_schema import make_result


class StoreTests(unittest.TestCase):
    def test_duplicate_results_are_ignored(self):
        store = Store()
        r = make_result()
        self.assertTrue(store.insert_result(r))
        self.assertFalse(store.insert_result(r))
        self.assertEqual(store.counts()["results"], 1)

    def test_results_roundtrip_ordered(self):
        store = Store()
        later = make_result(seq=2, timestamp=2000.0)
        earlier = make_result(seq=1, timestamp=1000.0, measurement=None, unit=None)
        store.insert_result(later)
        store.insert_result(earlier)
        self.assertEqual(store.results(), [earlier, later])

    def test_heartbeats_grouped_and_deduplicated(self):
        store = Store()
        store.insert_heartbeat(Heartbeat("st-01", 1, 10.0, "b"))
        store.insert_heartbeat(Heartbeat("st-01", 2, 20.0, "b"))
        self.assertFalse(store.insert_heartbeat(Heartbeat("st-01", 2, 20.0, "b")))
        self.assertEqual(store.heartbeats(), {"st-01": [10.0, 20.0]})

    def test_file_database_persists_after_close(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
           path = os.path.join(tmp, "t.db")
           store = Store(path)
           store.insert_result(make_result())
           store.close()

           reopened = Store(path)
           try:
              self.assertEqual(reopened.counts()["results"], 1)
           finally:
              reopened.close()


class IngestorTests(unittest.TestCase):
    def setUp(self):
        self.broker = InMemoryBroker()
        self.store = Store()
        self.ingestor = Ingestor(self.broker, self.store)
        self.ingestor.start()

    def test_valid_message_is_stored(self):
        self.broker.publish(results_topic("st-01"), make_result().to_payload())
        self.assertEqual(self.ingestor.stats.stored, 1)
        self.assertEqual(self.store.counts()["results"], 1)

    def test_redelivered_message_counts_as_duplicate(self):
        payload = make_result().to_payload()
        self.broker.publish(results_topic("st-01"), payload)
        self.broker.publish(results_topic("st-01"), payload)
        self.assertEqual(self.ingestor.stats.stored, 1)
        self.assertEqual(self.ingestor.stats.duplicates, 1)

    def test_invalid_messages_are_rejected_and_recorded(self):
        self.broker.publish(results_topic("st-01"), b"garbage")
        self.broker.publish(results_topic("st-02"), make_result().to_payload())  # spoofed id
        self.assertEqual(self.ingestor.stats.rejected, 2)
        self.assertEqual(self.store.counts()["rejected"], 2)
        self.assertEqual(self.store.counts()["results"], 0)
        reasons = " ".join(r["reason"] for r in self.store.rejected())
        self.assertIn("invalid JSON", reasons)
        self.assertIn("does not match topic", reasons)

    def test_heartbeats_are_stored(self):
        hb = Heartbeat("st-01", 1, 5.0, "b")
        self.broker.publish(heartbeat_topic("st-01"), hb.to_payload())
        self.assertEqual(self.store.heartbeats(), {"st-01": [5.0]})


if __name__ == "__main__":
    unittest.main()
