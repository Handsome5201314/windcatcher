"""Create a consistent SQLite snapshot of the operator activity history."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.activity_monitor import backup_database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="new SQLite backup file; existing files are not overwritten")
    args = parser.parse_args()
    print(backup_database(args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
