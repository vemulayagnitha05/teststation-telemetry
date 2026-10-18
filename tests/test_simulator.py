import json
import unittest

from telemetry.schema import ValidationError, parse_heartbeat, parse_result
from telemetry.simulator import FaultSpec, Simulator, default_faults


def collect(sim):
    return [(t, p) for _, msgs in sim.run() for t, p in msgs]


class SimulatorTests(unittest.TestCase):
    def test_same_seed_is_deterministic(self):
        a = collect(Simulator(n_ticks=30, seed=1, faults=default_faults(30)))
        b = collect(Simulator(n_ticks=30, seed=1, faults=default_faults(30)))
        c = collect(Simulator(n_ticks=30, seed=2, faults=default_faults(30)))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_healthy_messages_are_valid(self):
        for topic, payload in collect(Simulator(n_ticks=10)):
            if topic.endswith("/results"):
                parse_result(payload, topic)
            else:
                parse_heartbeat(payload, topic)

    def test_message_counts(self):
        sim = Simulator(n_stations=3, n_ticks=10)
        msgs = collect(sim)
        self.assertEqual(sum(t.endswith("/heartbeat") for t, _ in msgs), 30)
        self.assertEqual(sum(t.endswith("/results") for t, _ in msgs), 30 * len(sim.checks))

    def test_dropout_silences_station(self):
        sim = Simulator(n_stations=2, n_ticks=20,
                        faults=[FaultSpec("dropout", "st-02", 5, 10)])
        for tick, msgs in sim.run():
            stations = {t.split("/")[1] for t, _ in msgs}
            if 5 <= tick < 10:
                self.assertEqual(stations, {"st-01"})
            else:
                self.assertEqual(stations, {"st-01", "st-02"})

    def test_drift_moves_measurement(self):
        sim = Simulator(n_stations=1, n_ticks=100,
                        faults=[FaultSpec("drift", "st-01", 50, None, "tx_power", 0.1)])
        vals = [parse_result(p).measurement for t, p in collect(sim)
                if t.endswith("/results") and json.loads(p)["test_name"] == "tx_power"]
        # 0.1 sigma/tick * 0.3 sigma = 0.03 units/tick -> ~1.3 units by ticks 90-99
        self.assertGreater(sum(vals[90:]) / 10, sum(vals[:40]) / 40 + 1.0)

    def test_malformed_payloads_fail_validation(self):
        sim = Simulator(n_stations=1, n_ticks=10,
                        faults=[FaultSpec("malformed", "st-01", 2, 4)])
        bad = 0
        for topic, payload in collect(sim):
            if topic.endswith("/results"):
                try:
                    parse_result(payload, topic)
                except ValidationError:
                    bad += 1
        self.assertEqual(bad, 2)

    def test_ground_truth_has_timestamps(self):
        sim = Simulator(n_ticks=100, t0=5000.0, tick_s=10.0, faults=default_faults(100))
        gt = sim.ground_truth()
        drift = next(f for f in gt["faults"] if f["kind"] == "drift")
        self.assertEqual(drift["start_ts"], 5000.0 + 35 * 10.0)
        self.assertIsNone(drift["end_ts"])

    def test_unknown_fault_kind_rejected(self):
        with self.assertRaises(ValueError):
            FaultSpec("gremlins", "st-01", 0)


if __name__ == "__main__":
    unittest.main()
