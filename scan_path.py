"""Small maintenance entry point for indexing one path passed by the OS."""
from __future__ import annotations

import json
import sys

import app


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scan_path.py <folder>")
    path = app.clean_local_path(sys.argv[1])
    if not path.is_dir():
        raise SystemExit(f"Folder not found: {path}")
    app.init_db()
    with app.connect() as conn:
        conn.execute("DELETE FROM roots WHERE path=?", ("E:\\?????",))
        conn.execute(
            "INSERT OR IGNORE INTO roots(path,created_at) VALUES (?,?)",
            (str(path), app.now_ts()),
        )
    print(json.dumps(app.scan_directory(path), ensure_ascii=False))
