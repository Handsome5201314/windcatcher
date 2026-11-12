"""Read-only audit of SQLite activity backups in one explicit directory."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.activity_backup_audit import audit_directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="exact backup directory; not searched recursively")
    args = parser.parse_args()
    report = audit_directory(args.directory)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "current" else 2


if __name__ == "__main__":
    raise SystemExit(main())
