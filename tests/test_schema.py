import json
import unittest

from telemetry.schema import (Result, ValidationError, heartbeat_topic, parse_heartbeat,
                              parse_result, results_topic, station_from_topic)


def make_result(**overrides) -> Result:
    base = dict(station_id="st-01", seq=1, timestamp=1000.0, test_name="tx_power", suite="rf",
                build="b1", status="pass", duration_ms=12.5, measurement=20.1,
                lower_limit=18.5, upper_limit=21.5, unit="dBm")
    base.update(overrides)
    return Result(**base)


def payload(**overrides) -> bytes:
    obj = json.loads(make_result().to_payload())
    obj.update(overrides)
    return json.dumps(obj).encode()


class SchemaTests(unittest.TestCase):
    def test_roundtrip(self):
        r = make_result()
        self.assertEqual(parse_result(r.to_payload(), results_topic("st-01")), r)

    def test_optional_fields_may_be_missing(self):
        obj = json.loads(make_result().to_payload())
        for key in ("measurement", "lower_limit", "upper_limit", "unit"):
            obj.pop(key)
        r = parse_result(json.dumps(obj).encode())
        self.assertIsNone(r.measurement)
        self.assertFalse(r.out_of_limits())

    def test_rejects_invalid_json_and_non_objects(self):
        for bad in (b"not json", b"[1,2]", b"\xff\xfe", b""):
            with self.assertRaises(ValidationError):
                parse_result(bad)

    def test_rejects_bad_values(self):
        cases = [
            dict(status="weird"), dict(station_id=""), dict(seq=-1), dict(seq=1.5),
            dict(seq=True), dict(duration_ms=-1), dict(timestamp="now"),
            dict(measurement=True), dict(measurement="20"), dict(unit=5),
            dict(lower_limit=30.0, upper_limit=10.0),
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                parse_result(payload(**overrides))

    def test_rejects_non_finite_numbers(self):
        raw = payload().replace(b"20.1", b"NaN")
        with self.assertRaises(ValidationError):
            parse_result(raw)

    def test_rejects_missing_required_field(self):
        obj = json.loads(make_result().to_payload())
        del obj["build"]
        with self.assertRaises(ValidationError):
            parse_result(json.dumps(obj).encode())

    def test_topic_station_must_match_payload(self):
        with self.assertRaises(ValidationError):
            parse_result(make_result().to_payload(), results_topic("st-99"))

    def test_station_from_topic(self):
        self.assertEqual(station_from_topic("teststation/st-07/results"), "st-07")
        for bad in ("other/st-07/results", "teststation//results", "teststation/a"):
            with self.assertRaises(ValidationError):
                station_from_topic(bad)

    def test_out_of_limits(self):
        self.assertTrue(make_result(measurement=22.0).out_of_limits())
        self.assertTrue(make_result(measurement=18.0).out_of_limits())
        self.assertFalse(make_result(measurement=20.0).out_of_limits())

    def test_heartbeat_roundtrip_and_validation(self):
        from telemetry.schema import Heartbeat
        hb = Heartbeat("st-01", 3, 1000.0, "b1")
        self.assertEqual(parse_heartbeat(hb.to_payload(), heartbeat_topic("st-01")), hb)
        with self.assertRaises(ValidationError):
            parse_heartbeat(b'{"station_id": "st-01"}')


if __name__ == "__main__":
    unittest.main()
