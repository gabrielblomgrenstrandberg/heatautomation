"""Command-line tools for house configuration and learned parameters."""

import argparse
from pathlib import Path

from sqlalchemy import create_engine

from .config import config_hash, load_house_config
from .reconcile import reconcile_house, reset_room_parameters
from .startup_reset import recorded_house


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    reconcile_parser = subparsers.add_parser(
        "reconcile", help="reconcile physical configuration into SQL"
    )
    reconcile_parser.add_argument("--config", default="config/house.json")
    reconcile_parser.add_argument("--database", default="thermal_history.sqlite3")

    reset_parser = subparsers.add_parser(
        "reset-parameters", help="reset learned values to current configuration priors"
    )
    reset_parser.add_argument("--room", required=True)
    reset_parser.add_argument("--parameter")
    reset_parser.add_argument("--database", default="thermal_history.sqlite3")

    args = parser.parse_args(argv)
    engine = create_engine("sqlite:///" + str(Path(args.database)))
    try:
        if args.action == "reconcile":
            house_config = load_house_config(args.config)
            recorded = recorded_house(args.database)
            if recorded is not None and recorded != (str(house_config.house.id), config_hash(house_config)):
                raise RuntimeError(
                    "House configuration differs from the database. Restart the application "
                    "to reset the database and learned model before reconciliation.")
            reconcile_house(engine, house_config)
            print("House configuration reconciled.")
        else:
            reset_room_parameters(engine, args.room, args.parameter)
            print("Learned parameters reset to their current priors.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
