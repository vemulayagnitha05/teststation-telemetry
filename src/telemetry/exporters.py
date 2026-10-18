"""Exports: CSV for test-report-tool and JSONL incident records for the RCA agent."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone

from .detectors import Incident
from .schema import Result

CSV_COLUMNS = ["test_name", "suite", "build", "timestamp", "status", "duration_ms",
               "measurement", "lower_limit", "upper_limit", "unit", "station_id"]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_csv(results: list[Result], path: str) -> int:
    """Write results in the column layout used by test-report-tool (plus station_id)."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for r in results:
            writer.writerow([r.test_name, r.suite, r.build, iso(r.timestamp), r.status,
                             r.duration_ms,
                             "" if r.measurement is None else r.measurement,
                             "" if r.lower_limit is None else r.lower_limit,
                             "" if r.upper_limit is None else r.upper_limit,
                             r.unit or "", r.station_id])
    return len(results)


def incident_records(incidents: list[Incident], results: list[Result],
                     history_len: int = 30) -> list[dict]:
    series: dict[tuple[str, str], list[Result]] = defaultdict(list)
    for r in results:
        series[(r.station_id, r.test_name)].append(r)
    for s in series.values():
        s.sort(key=lambda r: (r.timestamp, r.seq))

    records = []
    for n, inc in enumerate(incidents, 1):
        alarm_ts = inc.evidence.get("alarm_ts", inc.start_ts)
        history = []
        if inc.test_name:
            history = [r.status for r in series.get((inc.station_id, inc.test_name), [])
                       if r.timestamp <= alarm_ts][-history_len:]
        log_lines = [f"[{iso(inc.start_ts)}] {inc.summary}"]
        log_lines += [f"  {k}: {v}" for k, v in sorted(inc.evidence.items())]
        records.append({
            "id": f"tel-{n:04d}",
            "source": "teststation-telemetry",
            "station_id": inc.station_id,
            "test_name": inc.test_name,
            "detected_kind": inc.kind,
            "started_at": iso(inc.start_ts),
            "severity": inc.severity,
            "log": "\n".join(log_lines),
            "history": history,
            "evidence": inc.evidence,
        })
    return records


def write_incidents_jsonl(records: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
