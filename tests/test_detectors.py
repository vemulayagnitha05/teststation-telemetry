import random
import unittest

from telemetry.detectors import (compute_cpk, cpk_rating, cpk_table, detect_drift,
                                 detect_dropouts, detect_flaky, run_detectors)
from telemetry.schema import Result


def series(station="st-01", test="tx_power", values=None, statuses=None, lower=18.5,
           upper=21.5, step=10.0):
    values = values if values is not None else [None] * len(statuses)
    out = []
    for i, v in enumerate(values):
        if statuses:
            status = statuses[i]
        else:
            oob = v is not None and (v < lower or v > upper)
            status = "fail" if oob else "pass"
        out.append(Result(station, i + 1, 1000.0 + i * step, test, "rf", "b", status, 10.0,
                          v, lower if v is not None else None,
                          upper if v is not None else None, "dBm" if v is not None else None))
    return out


def noise(n, rng, mu=20.0, sigma=0.3):
    return [rng.gauss(mu, sigma) for _ in range(n)]


class DriftTests(unittest.TestCase):
    def test_detects_ramp_and_estimates_onset(self):
        rng = random.Random(1)
        vals = noise(100, rng) + [20.0 + 0.03 * i + rng.gauss(0, 0.3) for i in range(1, 101)]
        incidents = detect_drift(series(values=vals))
        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc.kind, "drift")
        self.assertEqual(inc.evidence["direction"], "up")
        onset_index = round((inc.start_ts - 1000.0) / 10.0)
        self.assertTrue(80 <= onset_index <= 140, onset_index)

    def test_detects_downward_step(self):
        rng = random.Random(2)
        vals = noise(100, rng) + noise(60, rng, mu=19.0)
        inc = detect_drift(series(values=vals))[0]
        self.assertEqual(inc.evidence["direction"], "down")

    def test_stationary_noise_rarely_alarms(self):
        false_alarms = 0
        for seed in range(300):
            rng = random.Random(seed)
            if detect_drift(series(values=noise(300, rng))):
                false_alarms += 1
        self.assertLessEqual(false_alarms, 3, f"{false_alarms}/300 healthy series flagged")

    def test_too_short_series_is_skipped(self):
        rng = random.Random(3)
        self.assertEqual(detect_drift(series(values=noise(30, rng, mu=25.0))), [])

    def test_series_without_measurements_is_ignored(self):
        self.assertEqual(detect_drift(series(statuses=["pass"] * 120)), [])

    def test_constant_baseline_does_not_crash(self):
        vals = [20.0] * 60 + [21.0] * 20
        self.assertEqual(len(detect_drift(series(values=vals))), 1)


class FlakyTests(unittest.TestCase):
    def test_intermittent_failures_are_flaky(self):
        rng = random.Random(4)
        statuses = ["fail" if rng.random() < 0.3 else "pass" for _ in range(200)]
        incidents = detect_flaky(series(test="handover", statuses=statuses))
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].kind, "flaky")

    def test_contiguous_failure_block_is_not_flaky(self):
        statuses = ["pass"] * 100 + ["fail"] * 40 + ["pass"] * 60
        self.assertEqual(detect_flaky(series(test="handover", statuses=statuses)), [])

    def test_rare_random_failures_are_not_flaky(self):
        statuses = ["pass"] * 200
        for i in (20, 90, 150):
            statuses[i] = "fail"
        self.assertEqual(detect_flaky(series(test="handover", statuses=statuses)), [])

    def test_out_of_limit_failures_are_excluded(self):
        # measurements alternate in/out of limits: failures are explained, not flaky
        vals = [20.0 if i % 2 else 25.0 for i in range(100)]
        self.assertEqual(detect_flaky(series(values=vals)), [])

    def test_too_few_runs(self):
        self.assertEqual(detect_flaky(series(test="h", statuses=["pass", "fail"] * 5)), [])


class DropoutTests(unittest.TestCase):
    def beats(self, skip=range(0), n=60):
        return [1000.0 + 10.0 * i for i in range(n) if i not in skip]

    def test_gap_detected(self):
        incidents = detect_dropouts({"st-01": self.beats(skip=range(20, 30))})
        self.assertEqual(len(incidents), 1)
        self.assertAlmostEqual(incidents[0].evidence["gap_s"], 110.0)
        self.assertIsNotNone(incidents[0].end_ts)

    def test_regular_beats_no_incident(self):
        self.assertEqual(detect_dropouts({"st-01": self.beats()}), [])

    def test_ongoing_dropout_when_now_is_later(self):
        beats = self.beats(n=40)
        incidents = detect_dropouts({"st-01": beats}, now=beats[-1] + 500)
        self.assertEqual(len(incidents), 1)
        self.assertIsNone(incidents[0].end_ts)

    def test_too_few_beats_skipped(self):
        self.assertEqual(detect_dropouts({"st-01": [1.0, 100.0, 200.0]}), [])


class CpkTests(unittest.TestCase):
    def test_known_value(self):
        values = [-1.0, 1.0] * 10  # mean 0, sample sd ~1.026
        cpk = compute_cpk(values, -3.0, 3.0)
        self.assertAlmostEqual(cpk, 3.0 / (3 * 1.0260), places=2)

    def test_one_sided_and_degenerate(self):
        self.assertIsNone(compute_cpk([1.0] * 20, 0.0, 2.0))  # zero variance
        self.assertIsNone(compute_cpk([1.0, 2.0], 0.0, 3.0))  # too few samples
        self.assertIsNone(compute_cpk([1.0, 2.0] * 10, None, None))
        self.assertIsNotNone(compute_cpk([1.0, 2.0] * 10, None, 10.0))

    def test_rating_bands(self):
        self.assertEqual(cpk_rating(1.5), "capable")
        self.assertEqual(cpk_rating(1.1), "marginal")
        self.assertEqual(cpk_rating(0.8), "poor")
        self.assertEqual(cpk_rating(None), "n/a")

    def test_table_worst_first(self):
        rng = random.Random(5)
        good = series(station="st-01", values=noise(60, rng))
        bad = series(station="st-02", values=noise(60, rng, mu=21.0, sigma=0.5))
        rows = cpk_table(good + bad)
        self.assertEqual(rows[0]["station_id"], "st-02")
        self.assertLess(rows[0]["cpk"], rows[1]["cpk"])


class RunDetectorsTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(run_detectors([], {}), [])


if __name__ == "__main__":
    unittest.main()
