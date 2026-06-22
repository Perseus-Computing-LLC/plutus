"""``python -m plutus_agent`` → the CLI."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
