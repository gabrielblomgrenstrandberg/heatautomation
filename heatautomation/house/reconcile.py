"""Idempotently reconcile physical configuration into the runtime database."""

from datetime import datetime, timezone

from sqlalchemy import delete, insert, select, update

from . import models
from .config import config_hash


def _insert_or_update(connection, table, key, values):
    existing = connection.execute(
        select(key).where(key == values[key.name])
    ).first()
    if existing is None:
        connection.execute(insert(table).values(**values))
    else:
        connection.execute(
            update(table).where(key == values[key.name]).values(
                **{name: value for name, value in values.items() if name != key.name}
            )
        )


def reconcile_house(engine, config):
    """Reconcile static entities, preserving runtime and learned database values."""
    now = datetime.now(timezone.utc)
    house_id = str(config.house.id)
    current_floor_ids = {str(item.id) for item in config.floors}
    current_room_ids = {str(item.id) for item in config.rooms}
    current_device_ids = {str(item.id) for item in config.devices}
    current_path_ids = {item.id for item in config.heat_paths}
    current_link_pairs = {
        tuple(sorted((str(link.room_a_id), str(link.room_b_id))))
        for link in config.thermal_links
    }
    digest = config_hash(config)

    with engine.begin() as connection:
        existing_house = connection.execute(select(
            models.houses.c.config_hash
        ).where(models.houses.c.id == house_id)).first()
        if existing_house is None:
            connection.execute(insert(models.houses).values(
                id=house_id,
                name=config.house.name,
                config_hash=digest,
                config_loaded_at=now,
            ))
        else:
            house_values = {"name": config.house.name}
            if existing_house.config_hash != digest:
                house_values.update(config_hash=digest, config_loaded_at=now)
            connection.execute(update(models.houses).where(
                models.houses.c.id == house_id
            ).values(**house_values))

        for floor in config.floors:
            floor_id = str(floor.id)
            _insert_or_update(connection, models.floors, models.floors.c.id, {
                "id": floor_id,
                "house_id": house_id,
                "name": floor.name,
                "level": floor.level,
                "removed_at": None,
            })

        for room in config.rooms:
            room_id = str(room.id)
            _insert_or_update(connection, models.rooms, models.rooms.c.id, {
                "id": room_id,
                "floor_id": str(room.floor_id),
                "name": room.name,
                "removed_at": None,
            })
            if connection.execute(
                select(models.room_settings.c.room_id).where(
                    models.room_settings.c.room_id == room_id
                )
            ).first() is None:
                connection.execute(insert(models.room_settings).values(
                    room_id=room_id, comfort_profile="default", updated_at=now
                ))

        for device in config.devices:
            device_id = str(device.id)
            _insert_or_update(connection, models.devices, models.devices.c.id, {
                "id": device_id,
                "kind": device.kind,
                "name": device.name,
                "room_id": str(device.room_id) if device.room_id else None,
                "adapter": device.adapter,
                "external_id": device.external_id,
                "rated_kw": device.rated_kw,
                "rated_w_at_dt50": device.rated_w_at_dt50,
                "provenance": device.provenance,
                "controlled_by": (
                    str(device.controlled_by) if device.controlled_by else None
                ),
                "removed_at": None,
            })
            connection.execute(delete(models.device_capabilities).where(
                models.device_capabilities.c.device_id == device_id
            ))
            if device.capabilities:
                connection.execute(insert(models.device_capabilities), [
                    {"device_id": device_id, "capability": capability}
                    for capability in sorted(set(device.capabilities))
                ])

        for path in config.heat_paths:
            path_type = "circuit" if path.emitter_ids else "direct"
            _insert_or_update(connection, models.heat_paths, models.heat_paths.c.id, {
                "id": path.id,
                "generator_id": str(path.generator_id),
                "path_type": path_type,
                "target_room_id": (
                    str(path.target_room_id) if path.target_room_id else None
                ),
                "distribution": path.distribution,
                "provenance": path.provenance,
                "removed_at": None,
            })
            connection.execute(delete(models.heat_path_emitters).where(
                models.heat_path_emitters.c.heat_path_id == path.id
            ))
            if path.emitter_ids:
                connection.execute(insert(models.heat_path_emitters), [
                    {"heat_path_id": path.id, "emitter_id": str(emitter_id)}
                    for emitter_id in path.emitter_ids
                ])

        for prior in config.room_priors:
            room_id = str(prior.room_id)
            for name, value in (
                ("capacity_j_per_k", prior.capacity_j_per_k),
                ("ua_w_per_k", prior.ua_w_per_k),
            ):
                _reconcile_parameter(connection, room_id, name, value, now)

        for link in config.thermal_links:
            room_a, room_b = sorted((str(link.room_a_id), str(link.room_b_id)))
            existing_link = connection.execute(select(
                models.thermal_links.c.room_a_id
            ).where(
                (models.thermal_links.c.room_a_id == room_a)
                & (models.thermal_links.c.room_b_id == room_b)
            )).first()
            if existing_link is None:
                connection.execute(models.thermal_links.insert().values(
                    room_a_id=room_a,
                    room_b_id=room_b,
                    conductance_w_per_k=link.conductance_w_per_k,
                    prior_conductance_w_per_k=link.conductance_w_per_k,
                    provenance=link.provenance,
                    removed_at=None,
                ))
            else:
                connection.execute(update(models.thermal_links).where(
                    (models.thermal_links.c.room_a_id == room_a)
                    & (models.thermal_links.c.room_b_id == room_b)
                ).values(
                    prior_conductance_w_per_k=link.conductance_w_per_k,
                    provenance=link.provenance,
                    removed_at=None,
                ))
            _reconcile_parameter(
                connection,
                room_a,
                "conductance_w_per_k:" + room_b,
                link.conductance_w_per_k,
                now,
            )

        for link in connection.execute(select(
                models.thermal_links.c.room_a_id,
                models.thermal_links.c.room_b_id)).all():
            if (link.room_a_id, link.room_b_id) not in current_link_pairs:
                connection.execute(update(models.thermal_links).where(
                    (models.thermal_links.c.room_a_id == link.room_a_id)
                    & (models.thermal_links.c.room_b_id == link.room_b_id)
                    & models.thermal_links.c.removed_at.is_(None)
                ).values(removed_at=now))

        _mark_removed(connection, models.floors, current_floor_ids)
        _mark_removed(connection, models.rooms, current_room_ids)
        _mark_removed(connection, models.devices, current_device_ids)
        _mark_removed(connection, models.heat_paths, current_path_ids)


def _reconcile_parameter(connection, room_id, name, prior_value, now):
    key = (models.thermal_parameters.c.room_id == room_id) & (
        models.thermal_parameters.c.name == name
    )
    existing = connection.execute(
        select(models.thermal_parameters.c.room_id).where(key)
    ).first()
    if existing is None:
        connection.execute(insert(models.thermal_parameters).values(
            room_id=room_id,
            name=name,
            value=prior_value,
            prior_value=prior_value,
            sample_count=0,
            covariance=None,
            updated_at=now,
        ))
    else:
        current = connection.execute(select(
            models.thermal_parameters.c.prior_value
        ).where(key)).scalar_one()
        if current != prior_value:
            connection.execute(insert(models.thermal_parameter_events).values(
                room_id=room_id,
                name=name,
                old_value=current,
                new_value=prior_value,
                reason="prior_updated",
                occurred_at=now,
            ))
            connection.execute(update(models.thermal_parameters).where(key).values(
                prior_value=prior_value
            ))


def _mark_removed(connection, table, current_ids):
    rows = connection.execute(select(table.c.id)).scalars().all()
    for row_id in rows:
        if row_id not in current_ids:
            connection.execute(update(table).where(table.c.id == row_id).values(
                removed_at=datetime.now(timezone.utc)
            ).where(table.c.removed_at.is_(None)))


def reset_room_parameters(engine, room_id, parameter_name=None):
    """Restore one room's learned parameters to current config priors, with audit."""
    now = datetime.now(timezone.utc)
    condition = models.thermal_parameters.c.room_id == str(room_id)
    if parameter_name is not None:
        condition = condition & (models.thermal_parameters.c.name == parameter_name)

    with engine.begin() as connection:
        parameters = connection.execute(
            select(models.thermal_parameters).where(condition)
        ).mappings().all()
        if not parameters:
            raise ValueError("No matching learned thermal parameters")
        for parameter in parameters:
            connection.execute(insert(models.thermal_parameter_events).values(
                room_id=parameter["room_id"],
                name=parameter["name"],
                old_value=parameter["value"],
                new_value=parameter["prior_value"],
                reason="reset",
                occurred_at=now,
            ))
            connection.execute(update(models.thermal_parameters).where(
                (models.thermal_parameters.c.room_id == parameter["room_id"])
                & (models.thermal_parameters.c.name == parameter["name"])
            ).values(
                value=parameter["prior_value"],
                sample_count=0,
                covariance=None,
                updated_at=now,
            ))
