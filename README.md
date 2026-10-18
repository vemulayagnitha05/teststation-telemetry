# teststation-telemetry

[![CI](https://github.com/vemulayagnitha05/teststation-telemetry/actions/workflows/ci.yml/badge.svg)](https://github.com/vemulayagnitha05/teststation-telemetry/actions/workflows/ci.yml)

**Stack:** Python 3.11+, MQTT/Mosquitto, SQLite, Docker Compose, pytest, GitHub Actions

**Status:** Portfolio/learning project; Docker pipeline and CI verified.

A small telemetry pipeline I built to simulate how test station data could be collected, checked, stored, and analysed.
```
test stations (simulator) --MQTT--> Mosquitto --> ingestor (validate, dedupe) --> SQLite
                                                                                    |
                         HTML report  <--  detectors: drift / flaky / dropout / Cpk <--+
                         incidents.jsonl (input for rca-agent)    results.csv (input for test-report-tool)
```

This project is also connected to my other two projects: `test-report-tool` analyses test
results and `rca-agent` diagnoses failures. I wanted this project to simulate the part before
that, where the data is collected and incidents are detected. The simulator also injects known
faults, which gives me ground truth to check whether the detectors found the right problem.

This is a portfolio/learning project using simulated data, so the goal is to show how I approached the problem rather than build a production system.

## Quick start

### Without Docker (quick local demo)

```
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate

pip install -e .
python -m telemetry demo
```
Open `output/report.html`. The demo uses the same ingestion and detection code, but uses an in-memory broker instead of Mosquitto. A pre-generated example (simulated data) is in
`docs/sample_report.html`.

### With Docker (real MQTT broker)

```
docker compose up --build          # mosquitto + ingestor + simulator (simulator exits when done)
docker compose down -v             # reset: removes stored data
```

Run `down -v` between runs. Messages are deduplicated by `(station_id, seq)`, so re-running the
simulator against old data would mix two runs.

### Tests

```
pip install -e .
python -m pytest
```

Locally, this gives `59 passed, 1 skipped`. The skipped test is the real-broker integration test
when `MQTT_HOST` is not set.

## What it does

**Simulator** (`simulator.py`). I simulate 5 stations, with each station running 4 checks per tick
(`tx_power`, `rx_sensitivity`, `supply_current`, `handover`). Each tick publishes the test results
and a heartbeat. Time is simulated (tick N is
`t0 + N * tick_s`) so runs are fast and reproducible. In the Docker setup, the messages travel
over the real MQTT broker. Injected faults, with known start times kept as ground truth:

| Fault | Station | Effect |
| --- | --- | --- |
| `drift` | st-02 | `tx_power` mean ramps upward from 35% of the run |
| `flaky` | st-03 | `handover` fails randomly 35% of the time |
| `dropout` | st-04 | station is silent from 60% to 70% of the run |
| `malformed` | st-01 | a few invalid payloads (tests input validation) |

st-01 and st-05 are otherwise healthy, so false positives can be counted.

**Ingestor** (`ingestor.py`, `store.py`). This is where I validate the incoming messages, check
that the station in the payload matches the MQTT topic, reject invalid messages, and store the
valid ones in SQLite. Inserts are idempotent on `(station_id, seq)`, since MQTT QoS 1 can
redeliver messages.

**Detectors** (`detectors.py`). I kept the detectors simple and explainable:
| Detector | Method | Notes |
| --- | --- | --- |
| Drift | Two-sided CUSUM per (station, test), baseline = first 50 samples, `k=1.0`, `h=10` | reports estimated onset and whether limits were later crossed |
| Flaky | Moderate rate of *unexplained* failures (10-90%) plus frequent pass/fail alternation | failures with an out-of-limit measurement are excluded; a contiguous failure block is a regression and not flaky |
| Dropout | Heartbeat gap longer than 3x the station's median interval | also reports a station that never came back |
| Cpk | Per (station, test) over the last 50 measurements | shown in the report, not an incident |

**Outputs** (`report.py`, `exporters.py`). The project produces a self-contained HTML report with
incident cards and trend sparklines, `results.csv` for `test-report-tool`, and `incidents.jsonl`
for `rca-agent`. The incident records also include the log text and recent pass/fail history.
## Detector tuning and measured results

I started with common CUSUM settings (`k=0.5`, `h=5-8`, baseline of 30 samples), but the demo
flagged two healthy stations. I then tested the detector on fault-free data and found that the
small baseline sample was causing too many false alarms. This is why I increased the baseline
and adjusted the CUSUM parameters.
False alarms on 2000 fault-free series of 300 samples, and detection of a 1.5-sigma step:

| baseline | k | h | false alarms | 1.5-sigma step: median delay |
| --- | --- | --- | --- | --- |
| 30 (MAD) | 0.5 | 8 | 35.3% | n/a |
| 50 | 0.5 | 8 | 13.4% | 7 samples |
| 50 | 1.0 | 8 | 0.15% | 14 samples |
| **50** | **1.0** | **10** | **0.05%** | **18 samples** |
| 100 | 1.0 | 10 | 0.00% | 18 samples |

The chosen setting is slower to react, but it gives a much lower false-alarm rate. A ramp of 0.1 sigma per tick is caught
after about 23 samples.

End-to-end on the default scenario, 200 simulated runs with different seeds (600 injected
faults): **0 missed, 2 false positives (about 1% of runs)**. Median drift-onset error is about
111 s (11 ticks); dropout onset is within one tick. These numbers are for *simulated Gaussian
data* and say nothing about real stations.

## Status: what is verified and what is not

| Part | Status |
| --- | --- |
| Schema validation, store, ingestor, simulator, detectors, evaluation, exports, report | Covered by 59 tests; all passing |
| Full pipeline through the in-memory broker, including detection evaluation | Passing |
| `PahoTransport` logic | Tested against a fake paho client |
| Docker Compose stack, Mosquitto, real `paho-mqtt` | Verified locally |
| Real MQTT data flow | Verified locally: 7,353 messages published, 7,350 stored and 3 rejected |
| SQLite persistence | Verified locally: 5,880 results, 1,470 heartbeats and 3 rejected messages |
| GitHub Actions CI | Passing on `main` |
| Integration test against a real broker | Environment-dependent; skipped unless `MQTT_HOST` is set |

The Docker Compose path and GitHub Actions workflow have been verified. The real-broker pytest
integration test is environment-dependent, so it is skipped locally unless `MQTT_HOST` is set.

## Design decisions

- **Idempotent ingestion.** QoS 1 is at-least-once, so duplicates are expected, not exceptional.
- **Validate messages before storing them.** Bad payloads are counted and kept for inspection
  instead of crashing the consumer or getting into the database unnoticed.
- **Device timestamps, not arrival time.** Drift and dropout detection use the timestamp in the
  message, so broker or network delay does not create false dropouts.
- **Transport abstraction.** I use an in-memory broker for fast, deterministic tests and
  `paho-mqtt` when running against a real broker. This lets me test most of the pipeline without
  needing Docker every time.
- **Ground truth from the simulator.** Detection quality is a number (precision, recall, onset
  error), and the e2e CI job fails if detections stop matching the injected faults.
- **Ingestor readiness.** The ingestor only reports healthy after its subscriptions are
  acknowledged, and the simulator waits for that, so no messages are lost at start-up.

## Connection to the other projects

-  I can pass `results.csv` to `test-report-tool` for pass-rate, flaky and Cpk analysis.
- `incidents.jsonl` holds one record per incident with `log`, `history`, `evidence` and the
  detector's `detected_kind`. I plan to use these fields as input for `rca-agent`, although I
  have not yet checked the field names against its `FailureRecord` schema. The simulator's
  ground truth is only suitable for testing the pipeline, not as labelled data for a thesis
  evaluation, which needs the manual labelling protocol described in `rca-agent`.

## Limitations

- Simulated, Gaussian, independent data. Real measurements are autocorrelated and non-normal.
- The CUSUM baseline must be fault-free. A drift that starts inside the first 50 samples is
  absorbed into the baseline and missed.
- Detectors run in batch on the stored data, not as a streaming service.
- SQLite is single-writer and fine for this scale, not for a real fleet. TimescaleDB or
  InfluxDB would be the next step.
- Mosquitto runs anonymous and unencrypted, and containers run as root. Production would need
  authentication, ACLs, TLS and a non-root user.
- The flaky detector needs at least 20 runs per series.

## Layout

```
src/telemetry/
  schema.py       message dataclasses, topics, strict validation
  transport.py    InMemoryBroker and PahoTransport
  store.py        SQLite storage, idempotent inserts
  ingestor.py     subscribe, validate, store, count
  simulator.py    stations, fault injection, ground truth
  detectors.py    drift, flaky, dropout, Cpk
  evaluate.py     detections vs ground truth
  exporters.py    CSV (test-report-tool) and JSONL (rca-agent)
  report.py       HTML report
  cli.py          simulate | ingest | analyze | demo
tests/            unit, pipeline and (optional) real-broker integration tests
docs/             sample_report.html generated by `telemetry demo`
mosquitto/        broker config
.github/workflows/ci.yml
Dockerfile  docker-compose.yml
```

## Roadmap

- Connect `incidents.jsonl` to `rca-agent` and evaluate how well it identifies the injected faults.
- Replace SQLite with TimescaleDB and add a Grafana dashboard.
- Stream the detectors instead of running them in batch.
