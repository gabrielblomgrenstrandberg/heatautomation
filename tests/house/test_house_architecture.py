import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, insert, inspect, select, update

from heatautomation.devices.command_gate import CommandGate
from heatautomation.house import models
from heatautomation.house.config import HouseConfig, load_house_config
from heatautomation.house.migrate import migrate_database
from heatautomation.house.reconcile import reconcile_house, reset_room_parameters
from heatautomation.learning.database import (
    metadata as legacy_metadata,
    observations,
    sensors,
)
from heatautomation.thermal.model import room_heat_demand_w, room_temperature_after_step
from heatautomation.thermal.observer import correct_temperature


EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "house.example.json"


class HouseArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "thermal.sqlite3"
        migrate_database(self.path, backup=False)
        self.engine = create_engine("sqlite:///" + str(self.path))
        self.addCleanup(self.engine.dispose)

    def test_example_config_validates_and_references_resolve(self):
        config = load_house_config(EXAMPLE_CONFIG)
        self.assertEqual(len(config.floors), 2)
        self.assertEqual(len(config.rooms), 11)
        self.assertEqual(len(config.devices), 24)

        data = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        data["rooms"][0]["floor_id"] = "308bbf0b-a260-4155-b221-1bcea6a94540"
        with self.assertRaises(ValueError):
            HouseConfig.model_validate(data)

    def test_discovered_mill_identity_cannot_be_assigned_twice(self):
        data = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        mill = next(device for device in data["devices"] if device["adapter"] == "mill")
        mill["external_id"] = "mill:001122334455"
        duplicate = dict(mill, id="b333adf4-f78e-4e98-8fc7-4050d20d709a")
        data["devices"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "only be assigned to one"):
            HouseConfig.model_validate(data)

    def test_reconciliation_is_idempotent_and_preserves_learned_values(self):
        config = load_house_config(EXAMPLE_CONFIG)
        reconcile_house(self.engine, config)
        with self.engine.connect() as connection:
            config_loaded_at = connection.execute(select(
                models.houses.c.config_loaded_at
            )).scalar_one()
        first_counts = {}
        for table in (models.floors, models.rooms, models.devices,
                      models.heat_paths, models.thermal_parameters):
            with self.engine.connect() as connection:
                first_counts[table.name] = len(
                    connection.execute(select(table)).all()
                )

        room_id = str(config.room_priors[0].room_id)
        with self.engine.begin() as connection:
            connection.execute(update(models.thermal_parameters).where(
                (models.thermal_parameters.c.room_id == room_id)
                & (models.thermal_parameters.c.name == "capacity_j_per_k")
            ).values(value=12345678, sample_count=7))

        reconcile_house(self.engine, config)
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(
                    models.houses.c.config_loaded_at
                )).scalar_one(),
                config_loaded_at,
            )
            learned = connection.execute(select(
                models.thermal_parameters.c.value,
                models.thermal_parameters.c.sample_count,
            ).where(
                (models.thermal_parameters.c.room_id == room_id)
                & (models.thermal_parameters.c.name == "capacity_j_per_k")
            )).one()
            for table in (models.floors, models.rooms, models.devices,
                          models.heat_paths, models.thermal_parameters):
                self.assertEqual(
                    len(connection.execute(select(table)).all()),
                    first_counts[table.name],
                )
        self.assertEqual(learned.value, 12345678)
        self.assertEqual(learned.sample_count, 7)

        updated_data = config.model_dump(mode="json")
        updated_data["room_priors"][0]["capacity_j_per_k"] = 9000000
        updated_config = HouseConfig.model_validate(updated_data)
        reconcile_house(self.engine, updated_config)
        with self.engine.connect() as connection:
            learned, prior = connection.execute(select(
                models.thermal_parameters.c.value,
                models.thermal_parameters.c.prior_value,
            ).where(
                (models.thermal_parameters.c.room_id == room_id)
                & (models.thermal_parameters.c.name == "capacity_j_per_k")
            )).one()
            events = connection.execute(select(
                models.thermal_parameter_events.c.reason
            ).where(models.thermal_parameter_events.c.name == "capacity_j_per_k")).all()
        self.assertEqual(learned, 12345678)
        self.assertEqual(prior, 9000000)
        self.assertEqual([row.reason for row in events], ["prior_updated"])

    def test_removed_device_is_soft_deleted_and_reset_is_audited(self):
        config = load_house_config(EXAMPLE_CONFIG)
        reconcile_house(self.engine, config)
        room_id = str(config.room_priors[0].room_id)
        device_id = str(config.devices[0].id)
        reduced_data = config.model_dump(mode="json")
        reduced_data["devices"] = [
            item for item in reduced_data["devices"] if item["id"] != device_id
        ]
        reduced_data["heat_paths"] = [
            item for item in reduced_data["heat_paths"]
            if item["generator_id"] != device_id
        ]
        reduced = HouseConfig.model_validate(reduced_data)
        reconcile_house(self.engine, reduced)
        with self.engine.connect() as connection:
            removed_at = connection.execute(select(models.devices.c.removed_at).where(
                models.devices.c.id == device_id
            )).scalar_one()
        reconcile_house(self.engine, reduced)
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(models.devices.c.removed_at).where(
                    models.devices.c.id == device_id
                )).scalar_one(),
                removed_at,
            )
        self.assertIsNotNone(removed_at)

        reset_room_parameters(self.engine, room_id, "capacity_j_per_k")
        with self.engine.connect() as connection:
            parameter = connection.execute(select(
                models.thermal_parameters.c.value,
                models.thermal_parameters.c.prior_value,
            ).where(
                (models.thermal_parameters.c.room_id == room_id)
                & (models.thermal_parameters.c.name == "capacity_j_per_k")
            )).one()
            event_count = len(connection.execute(
                select(models.thermal_parameter_events)
            ).all())
        self.assertEqual(parameter.value, parameter.prior_value)
        self.assertEqual(event_count, 1)

    def test_simulation_subdivides_unstable_euler_step(self):
        from heatautomation.thermal.model import simulate_step

        result = simulate_step(
            temperatures_c={"a": 20.0, "b": 30.0},
            parameters={
                "a": {
                    "capacity_j_per_k": 1.0,
                    "ua_w_per_k": 0.0,
                    "conductances_w_per_k": {"b": 1.0},
                },
                "b": {
                    "capacity_j_per_k": 1.0,
                    "ua_w_per_k": 0.0,
                    "conductances_w_per_k": {"a": 1.0},
                },
            },
            heat_input_w={},
            outdoor_c=25.0,
            seconds=20.0,
        )
        self.assertGreater(result["a"], 20.0)
        self.assertLessEqual(result["a"], 25.0)
        self.assertGreaterEqual(result["b"], 25.0)
        self.assertLessEqual(result["b"], 30.0)

    def test_test_mode_command_is_audited_without_dispatch(self):
        config = load_house_config(EXAMPLE_CONFIG)
        reconcile_house(self.engine, config)
        dispatched = []
        gate = CommandGate(
            self.engine,
            test_mode=True,
            automatic_control_enabled=True,
            dispatchers={"set_power": lambda *args: dispatched.append(args)},
        )
        result = gate.request(str(config.devices[0].id), "set_power", {"on": True})
        self.assertEqual(result.outcome, "simulated")
        self.assertEqual(dispatched, [])
        with self.engine.connect() as connection:
            row = connection.execute(select(models.command_log)).one()
        self.assertEqual(row.outcome, "simulated")

    def test_thermal_balance_demand_and_observer(self):
        next_c = room_temperature_after_step(
            room_id="living",
            temperatures_c={"living": 20.0, "kitchen": 22.0},
            capacity_j_per_k=1_000_000,
            ua_w_per_k=0,
            conductances_w_per_k={"kitchen": 100},
            heat_input_w=0,
            outdoor_c=0,
            seconds=3600,
        )
        self.assertAlmostEqual(next_c, 20.72)
        self.assertAlmostEqual(
            room_heat_demand_w(
                current_c=20,
                target_c=21,
                outdoor_c=10,
                capacity_j_per_k=1_000_000,
                ua_w_per_k=50,
                conductance_by_neighbor={},
                temperature_by_room={},
                response_seconds=3600,
            ),
            777.7777777778,
        )
        self.assertEqual(correct_temperature(None, None, "stale"), (None, "unknown"))
        estimate, classification = correct_temperature(19, 20, "good", 0.5)
        self.assertEqual(estimate, 19.5)
        self.assertEqual(classification, "estimated")


class MigrationTests(unittest.TestCase):
    def test_existing_legacy_database_is_backed_up_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "thermal.sqlite3"
            engine = create_engine("sqlite:///" + str(path))
            legacy_metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(insert(
                    observations
                ).values(id=1, timestamp="2026-01-01T00:00:00+00:00"))
                connection.execute(insert(
                    sensors
                ).values(observation_id=1, name="indoor_c", value=19.5))
            engine.dispose()

            backup = migrate_database(path, backup=True)
            self.assertTrue(backup.exists())
            engine = create_engine("sqlite:///" + str(path))
            try:
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(select(sensors.c.value).where(
                            sensors.c.name == "indoor_c"
                        )).scalar_one(),
                        19.5,
                    )
                    self.assertTrue(inspect(connection).has_table("room_measurements"))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
