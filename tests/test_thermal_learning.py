import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from heatautomation import thermal_collection, thermal_database, thermal_features
from heatautomation import thermal_prediction, thermal_training


def history(count=900):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    temperature = 20.0
    result = []
    for i in range(count):
        state = (i // 150) % 3
        pump = 4.0 if state == 1 else 0.0
        stove = 5.0 if state == 2 else 0.0
        outdoor = 5 + 3 * math.sin(i / 90)
        result.append({
            "timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
            "indoor_c": temperature, "outdoor_c": outdoor,
            "heatpump_capacity_kw": pump, "pelletstove_capacity_kw": stove,
        })
        temperature += (-0.05 * (temperature - outdoor) + 0.12 * pump + 0.1 * stove) / 12
    return result


class ThermalLearningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = thermal_database.ThermalDatabase(
            Path(self.directory.name) / "history.sqlite3")
        self.addCleanup(self.database.engine.dispose)
        self.model = str(Path(self.directory.name) / "model.joblib")

    def test_collection_preserves_unknowns_and_actual_device_state(self):
        pod = {"result": {
            "acState": {"on": True, "mode": "heat", "targetTemperature": 21},
            "mainMeasurementsSensor": {"measurements": {"temperature": 19.5, "humidity": 40}},
        }}
        with patch.object(thermal_collection.weather, "get_forecast",
                          return_value=[{"temperature_c": -1.5}]), \
                patch.object(thermal_collection.config, "TIBBER_API_KEY", "test"), \
                patch.object(thermal_collection.tibber, "get_spot_price", return_value=0.8), \
                patch.object(thermal_collection.sensibo, "status", return_value=pod), \
                patch.object(thermal_collection.kmp, "get_status", return_value={"mode": "LADDAR"}):
            thermal_collection.collect(self.database, lambda t: 4.0)
        row = self.database.history()[0]
        self.assertEqual(row["outdoor_c"], -1.5)
        self.assertEqual(row["indoor_c"], 19.5)
        self.assertEqual(row["target_c"], 21)
        self.assertEqual(row["price_sek_kwh"], 0.8)
        self.assertTrue(row["heatpump_is_on"])
        self.assertEqual(row["heatpump_capacity_kw"], 4)
        self.assertTrue(row["pelletstove_is_on"])
        self.assertIsNone(row["pelletstove_capacity_kw"])
        self.assertIsNotNone(datetime.fromisoformat(row["timestamp"]).utcoffset())

    def test_cooling_mode_is_not_mislabeled_as_passive_cooling(self):
        pod = {"result": {"acState": {"on": True, "mode": "cool"}}}
        with patch.object(thermal_collection.weather, "get_forecast", return_value=None), \
                patch.object(thermal_collection.config, "TIBBER_API_KEY", None), \
                patch.object(thermal_collection.sensibo, "status", return_value=pod), \
                patch.object(thermal_collection.kmp, "get_status", return_value=None):
            thermal_collection.collect(self.database, lambda t: 4.0)
        row = self.database.history()[0]
        self.assertIsNone(row["heatpump_capacity_kw"])
        self.assertIsNone(row["pelletstove_capacity_kw"])
        self.assertIsNone(row["indoor_c"])
        self.assertIsNone(row["price_sek_kwh"])

    def test_rejects_naive_timestamps(self):
        with self.assertRaises(ValueError):
            self.database.save(datetime.now(), {}, {})

    def test_feature_filter_rejects_gaps_transitions_and_missing_values(self):
        records = history(450)
        rows = thermal_features.training_rows(records)
        self.assertGreater(len(rows), 300)
        times = set(rows["timestamp"].astype(str))
        transition = datetime.fromisoformat(records[150]["timestamp"])
        self.assertNotIn(str(transition), times)
        records[250]["indoor_c"] = None
        records[300]["timestamp"] = (datetime.fromisoformat(records[300]["timestamp"])
                                     + timedelta(minutes=2)).isoformat()
        filtered = thermal_features.training_rows(records)
        self.assertLess(len(filtered), len(rows))

    def test_training_persistence_metrics_and_prediction(self):
        for record in history():
            self.database.save(datetime.fromisoformat(record["timestamp"]), {
                "indoor_c": record["indoor_c"], "outdoor_c": record["outdoor_c"],
            }, {
                "heatpump": {"is_on": bool(record["heatpump_capacity_kw"]), "mode": "heat",
                             "capacity_kw": record["heatpump_capacity_kw"]},
                "pelletstove": {"is_on": bool(record["pelletstove_capacity_kw"]), "mode": "test",
                                "capacity_kw": record["pelletstove_capacity_kw"]},
            })
        self.assertTrue(thermal_training.train(self.database, self.model))
        with self.database.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(thermal_database.evaluations)).fetchall()), 1)
        predictor = thermal_prediction.ThermalPredictor(self.model)
        recent = [20.0] * 7
        plan = [{"heatpump": 4.0, "pelletstove": 0.0}] * 72
        result = predictor.forecast(recent, [5.0] * 72, plan, target_c=20.1)
        self.assertEqual(set(result["predictions"]), {1, 2, 3, 6})
        self.assertGreaterEqual(result["predictions"][6]["error_scale_c"], 0)
        self.assertLess(predictor.cooling_rate(recent, 5)["rate_c_per_hour"], 0)
        self.assertIn("rate_c_per_hour", predictor.heating_rate(recent, 5, 4, 0))
        self.assertIn("rate_per_nominal_kw", predictor.source_response("heatpump"))
        self.assertGreaterEqual(predictor.heating_demand(recent, 5)["estimated_kw"], 0)
        with self.assertRaises(thermal_prediction.PredictionUnavailable):
            predictor.forecast(recent, [5.0] * 72,
                               [{"heatpump": 4.0, "pelletstove": 5.0}] * 72)
        with self.assertRaises(ValueError):
            predictor.forecast(recent, [5.0], plan)

    def test_insufficient_data_does_not_publish_model(self):
        self.assertFalse(thermal_training.train(self.database, self.model))
        self.assertFalse(Path(self.model).exists())
        with self.assertRaises(thermal_prediction.PredictionUnavailable):
            thermal_prediction.ThermalPredictor(self.model)

    def test_incomplete_heating_cycle_is_not_used(self):
        records = history(300)
        ongoing = thermal_features.training_rows(records)
        self.assertTrue((ongoing["regime"] == "cooling").all())
        complete = thermal_features.training_rows(history(450))
        self.assertGreater((complete["regime"] == "heating").sum(), 100)


if __name__ == "__main__":
    unittest.main()
