from __future__ import annotations

import argparse
from collections.abc import Sequence
from getpass import getpass
from pathlib import Path

from heimdall.auth.secrets import AuthSecretError, initialize_admin_secrets


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Initialize Heimdall's single administrator credential files."
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="Absolute host directory to create for authentication secrets",
    )
    arguments = parser.parse_args(argv)
    try:
        password = getpass("Admin password: ")
        confirmation = getpass("Confirm admin password: ")
        initialize_admin_secrets(arguments.directory, password, confirmation)
    except AuthSecretError as error:
        parser.error(str(error))
    print(f"Administrator authentication initialized in {arguments.directory}")


if __name__ == "__main__":
    main()
