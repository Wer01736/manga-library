"""Extract only representative pages for the visual translation gate."""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path


SOURCE = Path(r"D:\下載\日文測試用")
WORK = Path(r"E:\日文翻譯用資料_需配對網站\工作區\多漫畫視覺校正")
INPUT = WORK / "input"
OUTPUT = WORK / "output"
VALID = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
BOOKS = [
    (
        "book01_40p",
        "[ホワイティッシュ・ブラッカー (ANDO人)] ぶちゅキスヘブンVol2治験編.zip",
    ),
    (
        "book02_45p",
        "[割り箸効果] 閉経寸前の崖っぷち行き遅れ熟女エルフに召喚された結果.zip",
    ),
    (
        "book03_51p",
        "[割り箸効果] 純真無垢な彼女のセックスは元彼仕込みの下品スタイル.zip",
    ),
    (
        "book04_67p",
        "[割り箸効果] 成人後に再開した母は底辺売女になっていました…….zip",
    ),
]


def main() -> None:
    INPUT.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for book_id, filename in BOOKS:
        source_zip = SOURCE / filename
        with zipfile.ZipFile(source_zip) as archive:
            images = [
                item
                for item in archive.infolist()
                if not item.is_dir() and Path(item.filename).suffix.lower() in VALID
            ]
            indexes = sorted({min(3, len(images) - 1), len(images) // 2, max(0, len(images) - 4)})
            for role, index in zip(("front", "middle", "back"), indexes):
                item = images[index]
                suffix = Path(item.filename).suffix.lower()
                output_name = f"{book_id}_{role}_{index + 1:04d}{suffix}"
                target = INPUT / output_name
                with archive.open(item) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                manifest.append(
                    {
                        "book_id": book_id,
                        "source_zip": filename,
                        "page_count": len(images),
                        "role": role,
                        "source_index": index,
                        "source_entry": item.filename,
                        "calibration_file": output_name,
                    }
                )
    (WORK / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"books": len(BOOKS), "samples": len(manifest), "work": str(WORK)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
