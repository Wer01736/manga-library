from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

from translated_zip_pipeline import image_members, prepare


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("work_root", type=Path)
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()

    inventory: list[tuple[int, Path]] = []
    for source_zip in args.source_dir.glob("*.zip"):
        with zipfile.ZipFile(source_zip) as archive:
            inventory.append((len(image_members(archive)), source_zip))
    inventory.sort(key=lambda item: (item[0], item[1].name.casefold()))

    selected = inventory[:args.count]
    if len(selected) < args.count:
        raise RuntimeError(f"只找到 {len(selected)} 個漫畫 ZIP")

    tasks = []
    for number, (pages, source_zip) in enumerate(selected, 1):
        task_dir = args.work_root / f"book{number:02d}_{pages}p"
        prepare(source_zip, task_dir)
        tasks.append({
            "number": number,
            "pages": pages,
            "source_zip": str(source_zip),
            "task_dir": str(task_dir),
            "input_dir": str(task_dir / "input"),
            "output_dir": str(task_dir / "output"),
        })

    batch_manifest = args.work_root / "batch_manifest.json"
    batch_manifest.write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"batch_manifest": str(batch_manifest), "tasks": tasks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
