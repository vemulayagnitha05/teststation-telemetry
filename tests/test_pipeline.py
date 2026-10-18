"""End-to-end through the in-memory broker: simulate -> ingest -> detect -> evaluate."""
import csv
import json
import os
import tempfile
import unittest

from telemetry.detectors import run_detectors
from telemetry.evaluate import evaluate
from telemetry.exporters import CSV_COLUMNS, incident_records, write_csv, write_incidents_jsonl
from telemetry.ingestor import Ingestor
from telemetry.report import render_report
from telemetry.simulator import FaultSpec, Simulator, default_faults
from telemetry.store import Store
from telemetry.transport import InMemoryBroker


def run_pipeline(seed=42, n_ticks=300, faults=None, n_stations=5):
    faults = default_faults(n_ticks) if faults is None else faults
    sim = Simulator(n_stations=n_stations, n_ticks=n_ticks, seed=seed, faults=faults)
    broker, store = InMemoryBroker(), Store()
    ingestor = Ingestor(broker, store)
    ingestor.start()
    for _, messages in sim.run():
        for topic, payload in messages:
            broker.publish(topic, payload)
    incidents = run_detectors(store.results(), store.heartbeats())
    return sim, store, ingestor, incidents


class PipelineTests(unittest.TestCase):
    def test_every_injected_fault_found_without_false_positives(self):
        sim, store, ingestor, incidents = run_pipeline()
        evaluation = evaluate(incidents, sim.ground_truth())
        self.assertEqual(evaluation["unexpected"], [])
        self.assertEqual(evaluation["recall"], 1.0)
        self.assertEqual(evaluation["precision"], 1.0)
        self.assertEqual(ingestor.stats.rejected, 3)  # the 3 malformed payloads
        self.assertEqual(ingestor.stats.duplicates, 0)

    def test_detection_quality_across_many_seeds(self):
        missed = false_pos = 0
        seeds = range(100, 130)
        for seed in seeds:
            sim, _, _, incidents = run_pipeline(seed=seed)
            ev = evaluate(incidents, sim.ground_truth())
            missed += ev["false_negatives"]
            false_pos += ev["false_positives"]
        # 3 detectable faults per run; allow at most 1 stray false positive over 30 runs
        self.assertEqual(missed, 0)
        self.assertLessEqual(false_pos, 1)

    def test_fault_free_run_has_no_incidents(self):
        _, _, ingestor, incidents = run_pipeline(faults=[])
        self.assertEqual(incidents, [])
        self.assertEqual(ingestor.stats.rejected, 0)

    def test_onset_estimates_are_reasonable(self):
        sim, _, _, incidents = run_pipeline()
        ev = evaluate(incidents, sim.ground_truth())
        by_kind = {d["kind"]: d for d in ev["details"]}
        # drift onset within 15 ticks, dropout gap starts at the last beat before it
        self.assertLessEqual(abs(by_kind["drift"]["onset_error_s"]), 150)
        self.assertLessEqual(abs(by_kind["dropout"]["onset_error_s"]), 20)

    def test_evaluation_flags_a_missed_fault(self):
        sim, _, _, incidents = run_pipeline()
        extra = sim.ground_truth()
        extra["faults"].append({"kind": "drift", "station_id": "st-05", "test_name": "tx_power",
                                "start_ts": 0.0})
        ev = evaluate(incidents, extra)
        self.assertEqual(ev["false_negatives"], 1)
        self.assertLess(ev["recall"], 1.0)

    def test_ongoing_dropout_at_end_of_run(self):
        faults = [FaultSpec("dropout", "st-02", 200, None)]
        _, _, _, incidents = run_pipeline(faults=faults)
        ongoing = [i for i in incidents if i.kind == "dropout" and i.end_ts is None]
        self.assertEqual([i.station_id for i in ongoing], ["st-02"])

    def test_exports_and_report(self):
        sim, store, _, incidents = run_pipeline()
        results = store.results()
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "r.csv")
            self.assertEqual(write_csv(results, csv_path), len(results))
            with open(csv_path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(list(rows[0].keys()), CSV_COLUMNS)
            self.assertTrue(rows[0]["timestamp"].endswith("Z"))

            records = incident_records(incidents, results)
            jsonl = os.path.join(tmp, "i.jsonl")
            write_incidents_jsonl(records, jsonl)
            with open(jsonl, encoding="utf-8") as fh:
                loaded = [json.loads(line) for line in fh]
            self.assertEqual(len(loaded), len(incidents))
            flaky = next(r for r in loaded if r["detected_kind"] == "flaky")
            self.assertEqual(flaky["test_name"], "handover")
            self.assertTrue(flaky["history"])
            self.assertIn("handover", flaky["log"])

        html = render_report(results, store.heartbeats(), incidents, store.counts(),
                             evaluate(incidents, sim.ground_truth()))
        self.assertIn("<svg", html)  # drift sparkline
        self.assertIn("st-03", html)
        self.assertIn("Precision 1.00", html)

    def test_report_escapes_html(self):
        from telemetry.detectors import Incident
        inc = Incident("drift", "<script>x</script>", None, 1.0, None, "high", "<b>boom</b>")
        html = render_report([], {}, [inc], {})
        self.assertNotIn("<script>x", html)
        self.assertIn("&lt;b&gt;boom", html)


if __name__ == "__main__":
    unittest.main()
