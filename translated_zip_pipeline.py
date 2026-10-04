from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

from PIL import Image


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", value)]


def image_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = [
        info for info in archive.infolist()
        if not info.is_dir() and Path(info.filename).suffix.casefold() in IMAGE_SUFFIXES
    ]
    return sorted(members, key=lambda info: natural_key(info.filename))


def prepare(source_zip: Path, task_dir: Path) -> None:
    if task_dir.exists():
        shutil.rmtree(task_dir)
    input_dir = task_dir / "input"
    output_dir = task_dir / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir()

    manifest: list[dict[str, object]] = []
    with zipfile.ZipFile(source_zip) as archive:
        members = image_members(archive)
        if not members:
            raise RuntimeError(f"ZIP 內沒有圖片：{source_zip}")
        width = max(4, len(str(len(members))))
        for index, info in enumerate(members, 1):
            suffix = Path(info.filename).suffix.casefold()
            destination = input_dir / f"{index:0{width}d}{suffix}"
            destination.write_bytes(archive.read(info))
            with Image.open(destination) as image:
                image.verify()
            manifest.append({
                "index": index,
                "source_name": info.filename,
                "input_name": destination.name,
            })

    (task_dir / "manifest.json").write_text(
        json.dumps({
            "source_zip": str(source_zip),
            "page_count": len(manifest),
            "pages": manifest,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"status": "prepared", "pages": len(manifest)}, ensure_ascii=False))


def finalize(task_dir: Path, final_zip: Path) -> None:
    manifest = json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))
    expected = int(manifest["page_count"])
    output_dir = task_dir / "output"
    images = sorted(
        [path for path in output_dir.iterdir() if path.suffix.casefold() in IMAGE_SUFFIXES],
        key=lambda path: natural_key(path.name),
    )
    if len(images) != expected:
        raise RuntimeError(f"輸出頁數錯誤：預期 {expected}，實際 {len(images)}")

    width = max(4, len(str(expected)))
    checked: list[tuple[Path, str]] = []
    for index, image_path in enumerate(images, 1):
        with Image.open(image_path) as image:
            image.verify()
        checked.append((image_path, f"{index:0{width}d}{image_path.suffix.casefold()}"))

    final_zip.parent.mkdir(parents=True, exist_ok=True)
    partial = final_zip.with_name(final_zip.name + ".partial")
    if partial.exists():
        partial.unlink()
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for image_path, archive_name in checked:
            archive.write(image_path, archive_name)

    with zipfile.ZipFile(partial) as archive:
        final_members = image_members(archive)
        if len(final_members) != expected:
            raise RuntimeError("成品 ZIP 二次驗證頁數不符")
        for info in final_members:
            if info.file_size <= 0:
                raise RuntimeError("成品 ZIP 包含空白檔案")
        if archive.testzip() is not None:
            raise RuntimeError("成品 ZIP 完整性檢查失敗")

    partial.replace(final_zip)
    shutil.rmtree(task_dir)
    print(json.dumps({
        "status": "complete",
        "pages": expected,
        "zip": str(final_zip),
        "temporary_files_removed": True,
    }, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("source_zip", type=Path)
    prepare_parser.add_argument("task_dir", type=Path)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("task_dir", type=Path)
    finalize_parser.add_argument("final_zip", type=Path)
    args = parser.parse_args()

    if args.command == "prepare":
        prepare(args.source_zip, args.task_dir)
    else:
        finalize(args.task_dir, args.final_zip)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
