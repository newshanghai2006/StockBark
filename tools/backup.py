"""Create a consistent SQLite backup, including a database currently in WAL mode."""

import sqlite3
import sys
from pathlib import Path


if len(sys.argv) != 3:
    sys.exit("Usage: python -m tools.backup SOURCE.sqlite3 DESTINATION.sqlite3")
source, destination = (Path(value).resolve() for value in sys.argv[1:])
if not source.is_file():
    sys.exit("Source database does not exist")
if destination.exists():
    sys.exit("Destination already exists; choose a new backup filename")
with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
    with sqlite3.connect(destination) as dst:
        src.backup(dst)
print("Backup saved:", destination)
