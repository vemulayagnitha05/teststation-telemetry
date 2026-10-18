"""Deterministic test-station simulator with fault injection and ground truth.

Time is *simulated*: tick N has timestamp t0 + N * tick_s. That keeps runs fast,
reproducible and independent of broker/network jitter, while the messages still
travel over real MQTT when a PahoTransport is used.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Iterator, Optional

from .schema import Heartbeat, Result, heartbeat_topic, results_topic


@dataclass(frozen=True)
class CheckSpec:
    name: str
    suite: str
    nominal: Optional[float]
    sigma: float
    lower: Optional[float]
    upper: Optional[float]
    unit: Optional[str]
    mean_duration_ms: float


DEFAULT_CHECKS = (
    CheckSpec("tx_power", "rf", 20.0, 0.30, 18.5, 21.5, "dBm", 850.0),
    CheckSpec("rx_sensitivity", "rf", -95.0, 0.80, -98.0, -91.0, "dBm", 1200.0),
    CheckSpec("supply_current", "power", 120.0, 3.0, 105.0, 135.0, "mA", 300.0),
    CheckSpec("handover", "protocol", None, 0.0, None, None, None, 2400.0),
)

FAULT_KINDS = ("drift", "flaky", "dropout", "malformed")


@dataclass(frozen=True)
class FaultSpec:
    """kind: drift (magnitude = sigma per tick), flaky (magnitude = fail probability),
    dropout (station silent in [start_tick, end_tick)), malformed (bad payloads)."""
    kind: str
    station_id: str
    start_tick: int
    end_tick: Optional[int] = None
    test_name: Optional[str] = None
    magnitude: float = 1.0

    def __post_init__(self) -> None:
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {self.kind!r}")

    def active(self, tick: int) -> bool:
        return tick >= self.start_tick and (self.end_tick is None or tick < self.end_tick)


def default_faults(n_ticks: int) -> list[FaultSpec]:
    """Scenario used by the demo, the docker e2e run and the tests.
    st-01 and st-05 stay healthy so false positives are measurable."""
    return [
        FaultSpec("drift", "st-02", int(n_ticks * 0.35), None, "tx_power", 0.10),
        FaultSpec("flaky", "st-03", 0, None, "handover", 0.35),
        FaultSpec("dropout", "st-04", int(n_ticks * 0.60), int(n_ticks * 0.70)),
        FaultSpec("malformed", "st-01", int(n_ticks * 0.50), int(n_ticks * 0.50) + 3),
    ]


class Simulator:
    def __init__(self, n_stations: int = 5, n_ticks: int = 300, tick_s: float = 10.0,
                 seed: int = 42, faults: Optional[list[FaultSpec]] = None,
                 checks: tuple[CheckSpec, ...] = DEFAULT_CHECKS, build: str = "build_201",
                 t0: float = 1_760_000_000.0, base_fail_prob: float = 0.01) -> None:
        self.station_ids = [f"st-{i:02d}" for i in range(1, n_stations + 1)]
        self.n_ticks = n_ticks
        self.tick_s = tick_s
        self.seed = seed
        self.faults = list(faults) if faults is not None else []
        self.checks = checks
        self.build = build
        self.t0 = t0
        self.base_fail_prob = base_fail_prob
        self._rngs = {s: random.Random(f"{seed}:{s}") for s in self.station_ids}
        self._res_seq = dict.fromkeys(self.station_ids, 0)
        self._hb_seq = dict.fromkeys(self.station_ids, 0)

    def _fault(self, kind: str, station_id: str, tick: int,
               test_name: Optional[str] = None) -> Optional[FaultSpec]:
        for f in self.faults:
            if (f.kind == kind and f.station_id == station_id and f.active(tick)
                    and (f.test_name is None or f.test_name == test_name)):
                return f
        return None

    def _make_result(self, sid: str, check: CheckSpec, tick: int, ts: float,
                     rng: random.Random) -> Result:
        measurement = None
        if check.nominal is not None:
            measurement = rng.gauss(check.nominal, check.sigma)
            drift = self._fault("drift", sid, tick, check.name)
            if drift is not None:
                measurement += drift.magnitude * check.sigma * (tick - drift.start_tick)
            measurement = round(measurement, 4)
        duration = max(1.0, rng.gauss(check.mean_duration_ms, 0.1 * check.mean_duration_ms))
        failed = rng.random() < self.base_fail_prob
        flaky = self._fault("flaky", sid, tick, check.name)
        if flaky is not None and rng.random() < flaky.magnitude:
            failed = True
        out_of_limits = measurement is not None and (
            (check.lower is not None and measurement < check.lower)
            or (check.upper is not None and measurement > check.upper))
        self._res_seq[sid] += 1
        return Result(
            station_id=sid, seq=self._res_seq[sid], timestamp=round(ts, 3),
            test_name=check.name, suite=check.suite, build=self.build,
            status="fail" if (failed or out_of_limits) else "pass",
            duration_ms=round(duration, 1), measurement=measurement,
            lower_limit=check.lower, upper_limit=check.upper, unit=check.unit)

    def messages_for_tick(self, tick: int) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        for sid in self.station_ids:
            if self._fault("dropout", sid, tick) is not None:
                continue
            rng = self._rngs[sid]
            base = self.t0 + tick * self.tick_s + rng.uniform(-0.2, 0.2)
            self._hb_seq[sid] += 1
            out.append((heartbeat_topic(sid),
                        Heartbeat(sid, self._hb_seq[sid], round(base, 3), self.build).to_payload()))
            for k, check in enumerate(self.checks):
                result = self._make_result(sid, check, tick, base + 1.0 + k, rng)
                out.append((results_topic(sid), result.to_payload()))
            if self._fault("malformed", sid, tick) is not None:
                bad = (b"not json at all" if tick % 2 else
                       b'{"station_id": "%s", "status": "weird"}' % sid.encode())
                out.append((results_topic(sid), bad))
        return out

    def run(self) -> Iterator[tuple[int, list[tuple[str, bytes]]]]:
        for tick in range(self.n_ticks):
            yield tick, self.messages_for_tick(tick)

    def ground_truth(self) -> dict:
        faults = []
        for f in self.faults:
            d = asdict(f)
            d["start_ts"] = self.t0 + f.start_tick * self.tick_s
            d["end_ts"] = (None if f.end_tick is None
                           else self.t0 + f.end_tick * self.tick_s)
            faults.append(d)
        return {"t0": self.t0, "tick_s": self.tick_s, "n_ticks": self.n_ticks,
                "seed": self.seed, "faults": faults}
