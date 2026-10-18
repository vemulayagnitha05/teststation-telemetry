"""Explainable detectors: drift (CUSUM), flaky tests, station dropouts, Cpk.

All detectors are plain functions over stored data so they are easy to unit test.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Optional

from .schema import Result


@dataclass
class Incident:
    kind: str  # "drift" | "flaky" | "dropout"
    station_id: str
    test_name: Optional[str]
    start_ts: float
    end_ts: Optional[float]
    severity: str  # "medium" | "high"
    summary: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _group(results: list[Result]) -> dict[tuple[str, str], list[Result]]:
    groups: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for r in results:
        groups[(r.station_id, r.test_name)].append(r)
    for series in groups.values():
        series.sort(key=lambda r: (r.timestamp, r.seq))
    return groups


def detect_drift(results: list[Result], baseline_n: int = 50, k: float = 1.0,
                 h: float = 10.0, min_points: int = 60) -> list[Incident]:
    """Two-sided CUSUM on each (station, test) measurement series.

    The first `baseline_n` samples are assumed fault-free and give the mean and standard
    deviation. `k` (allowance) and `h` (alarm threshold) are in sigma units. Defaults were
    chosen by simulation (see README): estimating the baseline from a small sample makes
    textbook values (k=0.5, h=5) raise false alarms on 25-35% of healthy series, whereas
    k=1.0, h=10 gave about 0.05% while still catching a 1.5-sigma step in ~18 samples.
    The reported onset is the last time the CUSUM statistic was zero before the alarm.
    Limitation: a drift that starts inside the baseline window is absorbed into it.
    """
    incidents: list[Incident] = []
    for (station, test), series in sorted(_group(results).items()):
        pts = [r for r in series if r.measurement is not None]
        if len(pts) < max(min_points, baseline_n + 10):
            continue
        base = [r.measurement for r in pts[:baseline_n]]
        mu = statistics.fmean(base)
        sigma = max(statistics.stdev(base), 1e-9, abs(mu) * 1e-6)
        hi = lo = 0.0
        hi_start = lo_start = baseline_n
        alarm = None
        for i in range(baseline_n, len(pts)):
            z = (pts[i].measurement - mu) / sigma
            if hi == 0.0:
                hi_start = i
            if lo == 0.0:
                lo_start = i
            hi = max(0.0, hi + z - k)
            lo = max(0.0, lo - z - k)
            if hi > h:
                alarm = (i, hi_start, "up")
                break
            if lo > h:
                alarm = (i, lo_start, "down")
                break
        if alarm is None:
            continue
        i, start_i, direction = alarm
        shifts = [(p.measurement - mu) / sigma for p in pts[start_i:i + 1]]
        shift = statistics.fmean(shifts)
        crossed = any(p.out_of_limits() for p in pts[start_i:])
        unit = f" {pts[0].unit}" if pts[0].unit else ""
        incidents.append(Incident(
            kind="drift", station_id=station, test_name=test,
            start_ts=pts[start_i].timestamp, end_ts=None,
            severity="high" if crossed else "medium",
            summary=(f"{test} on {station} drifted {direction} by about {abs(shift):.1f} sigma "
                     f"from its baseline of {mu:.3f}{unit}; alarm after "
                     f"{i - start_i + 1} samples"
                     + ("; measurements are now out of limits" if crossed else "")),
            evidence={"baseline_mean": round(mu, 4), "baseline_sigma": round(sigma, 4),
                      "direction": direction, "mean_shift_sigma": round(shift, 2),
                      "alarm_ts": pts[i].timestamp, "samples_to_alarm": i - start_i + 1,
                      "out_of_limits_after_onset": crossed}))
    return incidents


def detect_flaky(results: list[Result], min_runs: int = 20, min_rate: float = 0.10,
                 max_rate: float = 0.90, min_flip_ratio: float = 0.15) -> list[Incident]:
    """Flag tests whose failures are intermittent and not explained by measurement limits.

    Failures with an out-of-limit measurement are excluded (that is drift or a hard fault,
    not flakiness). A series is flaky when the rate of unexplained failures is moderate AND
    outcomes alternate often: a contiguous block of failures is a regression, not flakiness.
    """
    incidents: list[Incident] = []
    for (station, test), series in sorted(_group(results).items()):
        outcomes: list[tuple[float, int]] = []
        for r in series:
            if r.status == "pass":
                outcomes.append((r.timestamp, 0))
            elif r.status in ("fail", "error") and not r.out_of_limits():
                outcomes.append((r.timestamp, 1))
        n = len(outcomes)
        if n < min_runs:
            continue
        fails = sum(o for _, o in outcomes)
        rate = fails / n
        if not (min_rate <= rate <= max_rate):
            continue
        flips = sum(1 for a, b in zip(outcomes, outcomes[1:]) if a[1] != b[1])
        flip_ratio = flips / (n - 1)
        if flip_ratio < min_flip_ratio:
            continue
        first_fail = next(ts for ts, o in outcomes if o)
        incidents.append(Incident(
            kind="flaky", station_id=station, test_name=test,
            start_ts=first_fail, end_ts=None,
            severity="high" if rate >= 0.30 else "medium",
            summary=(f"{test} on {station} fails intermittently: {fails} of {n} runs "
                     f"({rate:.0%}) with {flips} pass/fail alternations"),
            evidence={"runs": n, "failures": fails, "failure_rate": round(rate, 3),
                      "alternations": flips, "flip_ratio": round(flip_ratio, 3),
                      "alarm_ts": outcomes[-1][0]}))
    return incidents


def detect_dropouts(heartbeats: dict[str, list[float]], factor: float = 3.0,
                    min_beats: int = 10, now: Optional[float] = None) -> list[Incident]:
    """Flag gaps longer than `factor` times the station's median heartbeat interval.

    If `now` is given, a station that has been silent since its last beat for longer
    than the threshold is reported as an ongoing dropout (end_ts is None).
    """
    incidents: list[Incident] = []
    for station, stamps in sorted(heartbeats.items()):
        ts = sorted(stamps)
        if len(ts) < min_beats:
            continue
        diffs = [b - a for a, b in zip(ts, ts[1:])]
        interval = statistics.median(diffs)
        if interval <= 0:
            continue
        threshold = factor * interval
        for a, b in zip(ts, ts[1:]):
            if b - a > threshold:
                incidents.append(Incident(
                    kind="dropout", station_id=station, test_name=None,
                    start_ts=a, end_ts=b, severity="high",
                    summary=(f"{station} sent no heartbeat for {b - a:.0f}s "
                             f"(expected every {interval:.0f}s, about "
                             f"{round((b - a) / interval) - 1} missed)"),
                    evidence={"gap_s": round(b - a, 1), "expected_interval_s": round(interval, 1),
                              "alarm_ts": b}))
        if now is not None and now - ts[-1] > threshold:
            incidents.append(Incident(
                kind="dropout", station_id=station, test_name=None,
                start_ts=ts[-1], end_ts=None, severity="high",
                summary=f"{station} has been silent for {now - ts[-1]:.0f}s and has not recovered",
                evidence={"gap_s": round(now - ts[-1], 1),
                          "expected_interval_s": round(interval, 1), "alarm_ts": now}))
    return incidents


def compute_cpk(values: list[float], lower: Optional[float], upper: Optional[float],
                min_n: int = 10) -> Optional[float]:
    if len(values) < min_n or (lower is None and upper is None):
        return None
    sd = statistics.stdev(values)
    if sd == 0:
        return None
    mean = statistics.fmean(values)
    parts = []
    if upper is not None:
        parts.append((upper - mean) / (3 * sd))
    if lower is not None:
        parts.append((mean - lower) / (3 * sd))
    return min(parts)


def cpk_rating(cpk: Optional[float]) -> str:
    if cpk is None:
        return "n/a"
    if cpk >= 1.33:
        return "capable"
    return "marginal" if cpk >= 1.0 else "poor"


def cpk_table(results: list[Result], window: int = 50) -> list[dict]:
    """Cpk per (station, test) over the most recent `window` measurements, worst first."""
    rows = []
    for (station, test), series in _group(results).items():
        pts = [r for r in series if r.measurement is not None][-window:]
        if not pts:
            continue
        last = pts[-1]
        cpk = compute_cpk([p.measurement for p in pts], last.lower_limit, last.upper_limit)
        rows.append({"station_id": station, "test_name": test, "n": len(pts), "cpk": cpk,
                     "rating": cpk_rating(cpk), "unit": last.unit})
    rows.sort(key=lambda r: (r["cpk"] is None, r["cpk"] if r["cpk"] is not None else 0.0,
                             r["station_id"], r["test_name"]))
    return rows


def run_detectors(results: list[Result], heartbeats: dict[str, list[float]]) -> list[Incident]:
    stamps = [r.timestamp for r in results] + [t for v in heartbeats.values() for t in v]
    now = max(stamps) if stamps else None
    incidents = (detect_drift(results) + detect_flaky(results)
                 + detect_dropouts(heartbeats, now=now))
    incidents.sort(key=lambda i: (i.start_ts, i.kind, i.station_id))
    return incidents
