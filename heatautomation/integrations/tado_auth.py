"""Authorize a Tado account without requiring a house configuration."""

import argparse
import sys
import time
from pathlib import Path

from heatautomation.integrations.tado import TadoError, TadoService, TokenFile


def main(argv=None):
    parser = argparse.ArgumentParser(description="Authorize the Tado Classic integration")
    parser.add_argument(
        "--token-file", type=Path,
        default=Path.home() / ".heatautomation" / "tado-token.json",
        help="refresh-token file in a private directory",
    )
    parser.add_argument(
        "--reauthorize", action="store_true",
        help="replace an existing token after completing a new authorization",
    )
    args = parser.parse_args(argv)
    service = None
    try:
        store = TokenFile(args.token_file)
        if not args.reauthorize and store.load():
            print("A Tado token already exists; use --reauthorize to replace it.")
            return 0
        service = TadoService(None, store)
        auth = service.begin_authentication()
        print("Open:", auth["verification_uri"], flush=True)
        print("Enter code:", auth["user_code"], flush=True)
        print("Waiting for authorization...", flush=True)
        while True:
            time.sleep(1)
            if service.poll_authentication():
                print("Tado authorization completed; refresh token saved.")
                return 0
    except TadoError as error:
        print("Tado authorization failed: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nTado authorization cancelled.", file=sys.stderr)
        return 130
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    sys.exit(main())
