"""Audited command dispatch with an unconditional TEST_MODE simulation path."""

import json
from datetime import datetime, timezone

from sqlalchemy import insert, select

from heatautomation.house.models import command_log, device_runtime_state


class CommandGate:
    def __init__(
        self,
        engine,
        test_mode=True,
        automatic_control_enabled=False,
        dispatchers=None,
    ):
        self.engine = engine
        self.test_mode = test_mode
        self.automatic_control_enabled = automatic_control_enabled
        self.dispatchers = dispatchers or {}

    def request(self, device_id, command, payload):
        if not isinstance(payload, dict):
            raise TypeError("Command payload must be a dictionary")
        serialized_payload = json.dumps(payload, allow_nan=False, sort_keys=True)

        if self.test_mode:
            return self._record(
                device_id, command, serialized_payload, "simulated",
                "TEST_MODE: command was not dispatched",
            )
        if not self.automatic_control_enabled:
            return self._record(
                device_id, command, serialized_payload, "rejected",
                "Automatic control is disabled",
            )

        with self.engine.connect() as connection:
            state = connection.execute(
                select(device_runtime_state.c.connectivity).where(
                    device_runtime_state.c.device_id == str(device_id)
                )
            ).scalar_one_or_none()
        if state != "ok":
            return self._record(
                device_id, command, serialized_payload, "rejected",
                "Device connectivity is not confirmed",
            )

        dispatcher = self.dispatchers.get(command)
        if dispatcher is None:
            return self._record(
                device_id, command, serialized_payload, "rejected",
                "No dispatcher is registered for this command",
            )
        try:
            result = dispatcher(str(device_id), payload)
        except Exception as error:
            self._record(
                device_id, command, serialized_payload, "failed",
                "%s: %s" % (type(error).__name__, error),
            )
            raise
        outcome = getattr(result, "outcome", None)
        detail = getattr(result, "detail", None)
        if outcome not in {"sent", "failed", "timeout", "rejected"}:
            raise ValueError("Dispatcher returned an invalid command outcome")
        return self._record(device_id, command, serialized_payload, outcome, detail)

    def _record(self, device_id, command, payload, outcome, detail):
        with self.engine.begin() as connection:
            connection.execute(insert(command_log).values(
                device_id=str(device_id),
                command=command,
                payload=payload,
                requested_at=datetime.now(timezone.utc),
                outcome=outcome,
                detail=detail,
            ))
        from .capabilities import CommandResult
        return CommandResult(outcome=outcome, detail=detail)
