"""Compare detected incidents with the simulator's injected faults (ground truth)."""
from __future__ import annotations

from typing import Optional

from .detectors import Incident

DETECTABLE = ("drift", "flaky", "dropout")


def _key(kind: str, station_id: str, test_name: Optional[str]) -> tuple:
    return (kind, station_id, None if kind == "dropout" else test_name)


def evaluate(incidents: list[Incident], ground_truth: dict) -> dict:
    truth = [f for f in ground_truth["faults"] if f["kind"] in DETECTABLE]
    found: dict[tuple, Incident] = {}
    for inc in incidents:
        found.setdefault(_key(inc.kind, inc.station_id, inc.test_name), inc)
    truth_keys = {_key(f["kind"], f["station_id"], f.get("test_name")) for f in truth}

    details = []
    for f in truth:
        inc = found.get(_key(f["kind"], f["station_id"], f.get("test_name")))
        row = {"kind": f["kind"], "station_id": f["station_id"],
               "test_name": f.get("test_name"), "detected": inc is not None,
               "onset_error_s": None}
        if inc is not None and f["kind"] in ("drift", "dropout"):
            row["onset_error_s"] = round(inc.start_ts - f["start_ts"], 1)
        details.append(row)

    tp = sum(1 for d in details if d["detected"])
    fn = len(details) - tp
    false_positives = [k for k in found if k not in truth_keys]
    fp = len(false_positives)
    return {
        "true_positives": tp, "false_negatives": fn, "false_positives": fp,
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "details": details,
        "unexpected": [{"kind": k[0], "station_id": k[1], "test_name": k[2]}
                       for k in false_positives],
    }
