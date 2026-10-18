# teststation-telemetry

[![CI](https://github.com/vemulayagnitha05/teststation-telemetry/actions/workflows/ci.yml/badge.svg)](https://github.com/vemulayagnitha05/teststation-telemetry/actions/workflows/ci.yml)

A small, containerised telemetry pipeline for **simulated production test stations**:

```
test stations (simulator) --MQTT--> Mosquitto --> ingestor (validate, dedupe) --> SQLite
                                                                                    |
                         HTML report  <--  detectors: drift / flaky / dropout / Cpk <--+
                         incidents.jsonl (input for rca-agent)    results.csv (input for test-report-tool)
```

It is the infrastructure side of my other two projects: `test-report-tool` analyses test
results and `rca-agent` diagnoses failures. This project is how those results would arrive in
the first place, and it produces incidents with a known cause so the diagnosis side can be
checked against ground truth.

It is a **portfolio / learning project on simulated data**, not a production system.

## Quick start

### Without Docker (in-process demo, about 10 seconds)

```
python -m venv venv
venv\Scripts\activate            # Linux/macOS: source venv/bin/activate
pip install -e .
python -m telemetry demo
```

Open `output/report.html`. The demo runs the same ingestion and detection code, but uses an
in-memory broker instead of Mosquitto. A pre-generated example (simulated data) is in
`docs/sample_report.html`.

### With Docker (real MQTT broker)

```
docker compose up --build          # mosquitto + ingestor + simulator (simulator exits when done)
docker compose run --rm analyzer   # writes output/report.html, incidents.jsonl, results.csv
docker compose down -v             # reset: removes stored data
```

Run `down -v` between runs. Messages are deduplicated by `(station_id, seq)`, so re-running the
simulator against old data would mix two runs.

### Tests

```
pip install -e .
python -m unittest discover -s tests -t . -v                     # 59 unit tests
docker compose up -d --wait mosquitto
MQTT_HOST=localhost python -m unittest tests.test_integration_mqtt -v   # real broker
```

## What it does

**Simulator** (`simulator.py`). 5 stations run 4 checks per tick (`tx_power`, `rx_sensitivity`,
`supply_current`, `handover`) and publish results plus a heartbeat. Time is simulated (tick N is
`t0 + N * tick_s`) so runs are fast and reproducible, while the messages still travel over real
MQTT. Injected faults, with known start times kept as ground truth:

| Fault | Station | Effect |
| --- | --- | --- |
| `drift` | st-02 | `tx_power` mean ramps upward from 35% of the run |
| `flaky` | st-03 | `handover` fails randomly 35% of the time |
| `dropout` | st-04 | station is silent from 60% to 70% of the run |
| `malformed` | st-01 | a few invalid payloads (tests input validation) |

st-01 and st-05 are otherwise healthy, so false positives can be counted.

**Ingestor** (`ingestor.py`, `store.py`). Validates every payload strictly (`schema.py`), checks
that the station in the payload matches the topic, rejects and records bad messages, and stores
the rest in SQLite. Inserts are idempotent on `(station_id, seq)`, because MQTT QoS 1 can
redeliver a message.

**Detectors** (`detectors.py`). All are plain, explainable functions:

| Detector | Method | Notes |
| --- | --- | --- |
| Drift | Two-sided CUSUM per (station, test), baseline = first 50 samples, `k=1.0`, `h=10` | reports estimated onset and whether limits were later crossed |
| Flaky | Moderate rate of *unexplained* failures (10-90%) plus frequent pass/fail alternation | failures with an out-of-limit measurement are excluded, a contiguous failure block is a regression and not flaky |
| Dropout | Heartbeat gap longer than 3x the station's median interval | also reports a station that never came back |
| Cpk | Per (station, test) over the last 50 measurements | shown in the report, not an incident |

**Outputs** (`report.py`, `exporters.py`). A self-contained HTML report with incident cards and
trend sparklines, `results.csv` in the column layout of `test-report-tool` (plus `station_id`),
and `incidents.jsonl` with a log text and recent pass/fail history per incident.

## Detector tuning and measured results

I first used textbook CUSUM settings (`k=0.5`, `h=5-8`, baseline of 30 samples). The demo then
flagged two healthy stations. Measuring on fault-free data showed why: estimating the baseline
from a small sample acts like a permanent bias, so false-alarm rates were far higher than the
theoretical tables suggest.

False alarms on 2000 fault-free series of 300 samples, and detection of a 1.5-sigma step:

| baseline | k | h | false alarms | 1.5-sigma step: median delay |
| --- | --- | --- | --- | --- |
| 30 (MAD) | 0.5 | 8 | 35.3% | n/a |
| 50 | 0.5 | 8 | 13.4% | 7 samples |
| 50 | 1.0 | 8 | 0.15% | 14 samples |
| **50** | **1.0** | **10** | **0.05%** | **18 samples** |
| 100 | 1.0 | 10 | 0.00% | 18 samples |

The chosen setting is slower to react but trustworthy. A ramp of 0.1 sigma per tick is caught
after about 23 samples.

End-to-end on the default scenario, 200 simulated runs with different seeds (600 injected
faults): **0 missed, 2 false positives (about 1% of runs)**. Median drift-onset error is about
111 s (11 ticks); dropout onset is within one tick. These numbers are for *simulated Gaussian
data* and say nothing about real stations.

## Status: what is verified and what is not

| Part | Status |
| --- | --- |
| Schema validation, store, ingestor, simulator, detectors, evaluation, exports, report | Covered by 59 unit tests, all passing |
| Full pipeline through the in-memory broker, including the 30-seed detection test | Passing |
| `PahoTransport` logic (subscribe, dispatch, reconnect, handler errors) | Tested against a **fake** paho client, not the real library |
| `docker compose` stack, Mosquitto, real `paho-mqtt` | **Not yet run.** Written but not executed in the environment where this was built |
| GitHub Actions workflow (lint, tests, integration, e2e, image release) | **Not yet run.** First push will show whether it passes |
| Integration test against a real broker | Written, skipped unless `MQTT_HOST` is set, not yet run |

To close the gaps: run the Docker quick start once, run the integration test, push to GitHub
and fix whatever the first CI run reports, then update this table with what actually happened.

## Design decisions

- **Idempotent ingestion.** QoS 1 is at-least-once, so duplicates are expected, not exceptional.
- **Strict validation at the boundary.** Bad payloads are counted and stored for inspection
  instead of crashing the consumer or silently polluting the database.
- **Device timestamps, not arrival time.** Drift and dropout detection use the timestamp in the
  message, so broker or network delay does not create false dropouts.
- **Transport abstraction.** The same ingestor runs on an in-memory broker (fast, deterministic
  tests) and on paho-mqtt (real broker). Only the latter needs Docker.
- **Ground truth from the simulator.** Detection quality is a number (precision, recall, onset
  error), and the e2e CI job fails if detections stop matching the injected faults.
- **Ingestor readiness.** The ingestor only reports healthy after its subscriptions are
  acknowledged, and the simulator waits for that, so no messages are lost at start-up.

## Connection to the other projects

- `results.csv` can be passed to `test-report-tool` for pass-rate, flaky and Cpk analysis.
- `incidents.jsonl` holds one record per incident with `log`, `history`, `evidence` and the
  detector's `detected_kind`. Map these fields onto the `FailureRecord` in `rca-agent`
  (`src/rca/schema.py`); I have not checked field names against that schema. The simulator's
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

- Run and fix the Docker and CI paths, then record results here.
- Feed `incidents.jsonl` into `rca-agent` and report its accuracy on these incidents.
- Replace SQLite with TimescaleDB and add a Grafana dashboard.
- Stream the detectors instead of running them in batch.
