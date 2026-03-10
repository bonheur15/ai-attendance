from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.storage import Storage


def main() -> None:
    storage = Storage(root="data")
    samples = [
        {"id": "S001", "name": "Alice Demo"},
        {"id": "S002", "name": "Bob Demo"},
    ]
    for item in samples:
        result = storage.upsert_identity(item["id"], item["name"])
        print(f"seeded {result['id']} -> {result['name']}")


if __name__ == "__main__":
    main()
