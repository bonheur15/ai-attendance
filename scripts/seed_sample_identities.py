from __future__ import annotations

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
