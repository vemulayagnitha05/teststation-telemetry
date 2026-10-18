"""Self-contained HTML report (no external assets)."""
from __future__ import annotations

from html import escape
from typing import Optional

from .detectors import Incident, cpk_table
from .exporters import iso
from .schema import Result

CSS = """
body{font-family:system-ui,Segoe UI,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;
color:#1b1f23}h1{margin-bottom:.2rem}h2{margin-top:2rem;border-bottom:1px solid #d0d7de;
padding-bottom:.3rem}table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #eaeef2}
th{background:#f6f8fa}.high{color:#b42318;font-weight:600}.medium{color:#b54708;font-weight:600}
.poor{color:#b42318}.marginal{color:#b54708}.capable{color:#067647}.muted{color:#57606a}
.card{border:1px solid #d0d7de;border-radius:8px;padding:.8rem 1rem;margin:.8rem 0}
code{background:#f6f8fa;padding:0 .25rem;border-radius:4px}
"""


def sparkline(values: list[float], lower: Optional[float], upper: Optional[float],
              onset_idx: Optional[int], width: int = 520, height: int = 80) -> str:
    if len(values) < 2:
        return ""
    pool = values + [x for x in (lower, upper) if x is not None]
    vmin, vmax = min(pool), max(pool)
    span = (vmax - vmin) or 1.0

    def y(v: float) -> float:
        return height - 4 - (v - vmin) / span * (height - 8)

    step = width / (len(values) - 1)
    points = " ".join(f"{i * step:.1f},{y(v):.1f}" for i, v in enumerate(values))
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
             f'role="img" aria-label="measurement trend">']
    for limit in (lower, upper):
        if limit is not None:
            parts.append(f'<line x1="0" x2="{width}" y1="{y(limit):.1f}" y2="{y(limit):.1f}" '
                         f'stroke="#b42318" stroke-dasharray="4 3"/>')
    if onset_idx is not None:
        parts.append(f'<line x1="{onset_idx * step:.1f}" x2="{onset_idx * step:.1f}" y1="0" '
                     f'y2="{height}" stroke="#6e7781" stroke-dasharray="2 3"/>')
    parts.append(f'<polyline points="{points}" fill="none" stroke="#0969da" stroke-width="1.5"/>')
    parts.append("</svg>")
    return "".join(parts)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def render_report(results: list[Result], heartbeats: dict[str, list[float]],
                  incidents: list[Incident], counts: dict[str, int],
                  evaluation: Optional[dict] = None,
                  title: str = "Test-station telemetry report") -> str:
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<title>{escape(title)}</title><style>{CSS}</style></head><body>",
           f"<h1>{escape(title)}</h1>"]
    if results:
        out.append(f"<p class='muted'>Data from {iso(min(r.timestamp for r in results))} "
                   f"to {iso(max(r.timestamp for r in results))}</p>")

    out.append("<h2>Ingestion</h2>")
    out.append(_table(["Stored results", "Stored heartbeats", "Rejected messages"],
                      [[str(counts.get("results", 0)), str(counts.get("heartbeats", 0)),
                        str(counts.get("rejected", 0))]]))

    stations: dict[str, list[Result]] = {}
    for r in results:
        stations.setdefault(r.station_id, []).append(r)
    rows = []
    for sid, rs in sorted(stations.items()):
        decided = [r for r in rs if r.status in ("pass", "fail", "error")]
        rate = sum(r.status == "pass" for r in decided) / len(decided) if decided else 0.0
        rows.append([escape(sid), str(len(rs)), f"{rate:.1%}",
                     str(len(heartbeats.get(sid, []))), iso(max(r.timestamp for r in rs))])
    out.append("<h2>Stations</h2>")
    out.append(_table(["Station", "Results", "Pass rate", "Heartbeats", "Last result"], rows))

    out.append(f"<h2>Incidents ({len(incidents)})</h2>")
    if not incidents:
        out.append("<p>No incidents detected.</p>")
    for inc in incidents:
        label = f"{inc.kind} | {inc.station_id}" + (f" | {inc.test_name}" if inc.test_name else "")
        card = [f"<div class='card'><b>{escape(label)}</b> "
                f"<span class='{inc.severity}'>{inc.severity}</span><br>"
                f"<span class='muted'>from {iso(inc.start_ts)}"
                + (f" to {iso(inc.end_ts)}" if inc.end_ts else " (ongoing)")
                + f"</span><p>{escape(inc.summary)}</p>"]
        if inc.kind == "drift" and inc.test_name:
            series = [r for r in results
                      if r.station_id == inc.station_id and r.test_name == inc.test_name
                      and r.measurement is not None]
            onset = next((i for i, r in enumerate(series) if r.timestamp >= inc.start_ts), None)
            card.append(sparkline([r.measurement for r in series], series[-1].lower_limit,
                                  series[-1].upper_limit, onset))
        card.append("</div>")
        out.append("".join(card))

    out.append("<h2>Process capability (Cpk, last 50 measurements)</h2>")
    out.append(_table(
        ["Station", "Test", "n", "Cpk", "Rating"],
        [[escape(r["station_id"]), escape(r["test_name"]), str(r["n"]),
          "n/a" if r["cpk"] is None else f"{r['cpk']:.2f}",
          f"<span class='{r['rating']}'>{r['rating']}</span>"] for r in cpk_table(results)]))

    if evaluation is not None:
        out.append("<h2>Detection vs injected faults (simulator ground truth)</h2>")
        out.append(f"<p>Precision {evaluation['precision']:.2f}, recall "
                   f"{evaluation['recall']:.2f} ({evaluation['true_positives']} found, "
                   f"{evaluation['false_negatives']} missed, "
                   f"{evaluation['false_positives']} unexpected).</p>")
        out.append(_table(
            ["Fault", "Station", "Test", "Detected", "Onset error (s)"],
            [[escape(d["kind"]), escape(d["station_id"]), escape(d["test_name"] or "-"),
              "yes" if d["detected"] else "<b class='high'>no</b>",
              "-" if d["onset_error_s"] is None else str(d["onset_error_s"])]
             for d in evaluation["details"]]))
    out.append("</body></html>")
    return "".join(out)
