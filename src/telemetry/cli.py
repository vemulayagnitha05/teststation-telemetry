"""Command line interface: simulate | ingest | analyze | demo."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from typing import Optional

from .detectors import run_detectors
from .evaluate import evaluate
from .exporters import incident_records, write_csv, write_incidents_jsonl
from .ingestor import Ingestor
from .report import render_report
from .simulator import Simulator, default_faults
from .store import Store
from .transport import InMemoryBroker, make_transport

log = logging.getLogger("telemetry")
READY_FILE = os.environ.get("READY_FILE",
                            os.path.join(tempfile.gettempdir(), "ingestor.ready"))


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _add_sim_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stations", type=int, default=5)
    p.add_argument("--ticks", type=int, default=300)
    p.add_argument("--tick-s", type=float, default=10.0, help="simulated seconds per tick")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--no-faults", action="store_true")


def _build_simulator(args: argparse.Namespace) -> Simulator:
    faults = [] if args.no_faults else default_faults(args.ticks)
    return Simulator(n_stations=args.stations, n_ticks=args.ticks, tick_s=args.tick_s,
                     seed=args.seed, faults=faults, t0=time.time() - args.ticks * args.tick_s)


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def cmd_simulate(args: argparse.Namespace) -> int:
    sim = _build_simulator(args)
    transport = make_transport(args.host, args.port, client_id="simulator")
    sent = 0
    try:
        for _, messages in sim.run():
            for topic, payload in messages:
                transport.publish(topic, payload)
                sent += 1
            time.sleep(args.delay_s)
        transport.flush()
    finally:
        transport.close()
    if args.ground_truth:
        _write_json(args.ground_truth, sim.ground_truth())
    log.info("simulator published %d messages from %d stations", sent, args.stations)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    store = Store(args.db)
    transport = make_transport(args.host, args.port, client_id="ingestor")
    ingestor = Ingestor(transport, store)
    ingestor.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with open(READY_FILE, "w", encoding="utf-8") as fh:
        fh.write("ready\n")
    log.info("ingestor ready, writing to %s", args.db)
    last_log = 0.0
    while not stop.wait(1.0):
        store.flush()
        if time.monotonic() - last_log > 10:
            log.info("stats: %s", ingestor.stats.as_dict())
            last_log = time.monotonic()
    transport.close()
    store.close()
    log.info("ingestor stopped, final stats: %s", ingestor.stats.as_dict())
    return 0


def _analyze(store: Store, out: str, incidents_path: Optional[str], csv_path: Optional[str],
             ground_truth: Optional[dict]) -> tuple[list, Optional[dict]]:
    results = store.results()
    heartbeats = store.heartbeats()
    incidents = run_detectors(results, heartbeats)
    evaluation = evaluate(incidents, ground_truth) if ground_truth else None
    html = render_report(results, heartbeats, incidents, store.counts(), evaluation)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    if incidents_path:
        write_incidents_jsonl(incident_records(incidents, results), incidents_path)
    if csv_path:
        write_csv(results, csv_path)
    return incidents, evaluation


def _print_summary(store: Store, incidents: list, evaluation: Optional[dict]) -> None:
    print(f"stored: {store.counts()}")
    for inc in incidents:
        print(f"  [{inc.severity}] {inc.kind:8s} {inc.station_id} {inc.test_name or '-':16s} "
              f"{inc.summary}")
    if evaluation:
        print(f"detection vs ground truth: precision={evaluation['precision']:.2f} "
              f"recall={evaluation['recall']:.2f} "
              f"(tp={evaluation['true_positives']} fp={evaluation['false_positives']} "
              f"fn={evaluation['false_negatives']})")


def cmd_analyze(args: argparse.Namespace) -> int:
    store = Store(args.db)
    gt = None
    if args.ground_truth:
        with open(args.ground_truth, encoding="utf-8") as fh:
            gt = json.load(fh)
    incidents, evaluation = _analyze(store, args.out, args.incidents, args.csv, gt)
    _print_summary(store, incidents, evaluation)
    print(f"report written to {args.out}")
    if args.check:
        if evaluation is None:
            print("--check needs --ground-truth", file=sys.stderr)
            return 2
        if evaluation["precision"] < 1.0 or evaluation["recall"] < 1.0:
            print("CHECK FAILED: detections do not match the injected faults", file=sys.stderr)
            return 1
        print("check passed: every injected fault detected, no false positives")
    store.close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Whole pipeline in one process (in-memory broker), no Docker or MQTT needed."""
    os.makedirs(args.out_dir, exist_ok=True)
    db = os.path.join(args.out_dir, "telemetry.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    store = Store(db)
    broker = InMemoryBroker()
    ingestor = Ingestor(broker, store)
    ingestor.start()
    sim = _build_simulator(args)
    for _, messages in sim.run():
        for topic, payload in messages:
            broker.publish(topic, payload)
    store.flush()
    gt = sim.ground_truth()
    _write_json(os.path.join(args.out_dir, "ground_truth.json"), gt)
    incidents, evaluation = _analyze(
        store, os.path.join(args.out_dir, "report.html"),
        os.path.join(args.out_dir, "incidents.jsonl"),
        os.path.join(args.out_dir, "results.csv"), gt)
    print(f"ingest stats: {ingestor.stats.as_dict()}")
    _print_summary(store, incidents, evaluation)
    print(f"outputs in {args.out_dir}/ (open report.html)")
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telemetry", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    host = _env("MQTT_HOST", "")
    port = int(_env("MQTT_PORT", "1883"))
    db = _env("DB_PATH", "telemetry.db")

    p = sub.add_parser("simulate", help="publish simulated station telemetry over MQTT")
    _add_sim_args(p)
    p.add_argument("--host", default=host)
    p.add_argument("--port", type=int, default=port)
    p.add_argument("--delay-s", type=float, default=0.02, help="wall-clock pause per tick")
    p.add_argument("--ground-truth", help="write the injected faults to this JSON file")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("ingest", help="subscribe, validate and store telemetry")
    p.add_argument("--host", default=host)
    p.add_argument("--port", type=int, default=port)
    p.add_argument("--db", default=db)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("analyze", help="run detectors on stored data and write a report")
    p.add_argument("--db", default=db)
    p.add_argument("--out", default="output/report.html")
    p.add_argument("--incidents", help="write incident records (JSONL) for the RCA agent")
    p.add_argument("--csv", help="export results as CSV for test-report-tool")
    p.add_argument("--ground-truth", help="simulator ground truth JSON for evaluation")
    p.add_argument("--check", action="store_true",
                   help="exit 1 unless detection matches ground truth exactly")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("demo", help="run the full pipeline in-process without Docker")
    _add_sim_args(p)
    p.add_argument("--out-dir", default="output")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)
