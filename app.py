from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import threading
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("COMIC_WEB_DATA", APP_DIR / "data"))
STATIC_DIR = APP_DIR / "static"
DB_PATH = DATA_DIR / "library.db"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
ARCHIVE_EXTENSIONS = {".zip", ".cbz"}
INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SIMILARITY_ALGORITHM_VERSION = 1
WNACG_HOSTS = {"wnacg.com", "www.wnacg.com", "wnacg.ru", "www.wnacg.ru"}
DOWNLOAD_RETRIES = 3
DOWNLOAD_REQUEST_TIMEOUT = 20
DOWNLOAD_TOTAL_TIMEOUT = 10 * 60
DOWNLOAD_WORKERS = 2
DOWNLOAD_ALBUM_CONCURRENCY = 2
DOWNLOAD_BATCH_PREVIEW_WORKERS = 4
DOWNLOAD_MAX_IMAGE_BYTES = 100 * 1024 * 1024
DOWNLOAD_JOB_STORE = DATA_DIR / "web_download_jobs.json"

download_jobs: dict[str, dict[str, Any]] = {}
download_jobs_lock = threading.Lock()
download_album_slots = threading.Semaphore(DOWNLOAD_ALBUM_CONCURRENCY)
duplicate_jobs: dict[str, dict[str, Any]] = {}
duplicate_jobs_lock = threading.Lock()

app = FastAPI(title="漫畫壓縮檔管理器", version="0.1.0")


@app.middleware("http")
async def prevent_stale_frontend_cache(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in {"/", "/index.html"} or path.endswith((".js", ".css")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def now_ts() -> int:
    return int(time.time())


def _download_job_record(job: dict[str, Any]) -> dict[str, Any]:
    """Return the small, restart-safe part of a download job.

    Threading objects are deliberately kept out of the file.  The URL and
    destination are enough to safely start the whole item again after a
    process interruption.
    """
    return {
        key: value
        for key, value in job.items()
        if not key.startswith("_")
        and key in {
            "id", "status", "title", "total", "completed", "message", "error",
            "archive_path", "archive_name", "source_url", "request_url", "root_path",
            "created_at", "finished_at", "retry_count", "cancel_requested",
        }
    }


def persist_download_jobs_locked() -> None:
    """Atomically save the download queue while ``download_jobs_lock`` is held."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    records = [_download_job_record(job) for job in download_jobs.values()]
    temporary = DOWNLOAD_JOB_STORE.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, DOWNLOAD_JOB_STORE)


def cleanup_interrupted_download(job: dict[str, Any]) -> None:
    """Remove only this job's incomplete staging files before a full retry."""
    root_value = job.get("root_path")
    job_id = str(job.get("id", ""))
    if not root_value or not job_id:
        return
    root = Path(root_value)
    temp_dir = root / f".comic-download-{job_id}"
    if temp_dir.is_dir():
        shutil.rmtree(temp_dir, ignore_errors=True)
    archive_name = str(job.get("archive_name", ""))
    if archive_name:
        partial = root / f".{Path(archive_name).stem}.{job_id}.partial"
        partial.unlink(missing_ok=True)


def restore_download_jobs() -> None:
    """Restore the queue and turn unfinished work into safe full-retry items."""
    if not DOWNLOAD_JOB_STORE.is_file():
        return
    try:
        records = json.loads(DOWNLOAD_JOB_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(records, list):
        return
    interrupted: list[dict[str, Any]] = []
    with download_jobs_lock:
        for record in records[-100:]:
            if not isinstance(record, dict) or not record.get("id"):
                continue
            job = dict(record)
            job.setdefault("retry_count", 0)
            job.setdefault("cancel_requested", False)
            job["_cancel_event"] = threading.Event()
            job["_user_cancelled"] = bool(job.get("cancel_requested"))
            if job.get("status") in {"queued", "running", "packing"}:
                if job["_user_cancelled"]:
                    job.update(status="cancelled", message="下載已由使用者取消", finished_at=now_ts())
                else:
                    job.update(
                        status="failed",
                        error="程式上次中斷，未完成項目不會續傳。",
                        message="上次下載中斷，請重新下載整個項目",
                        finished_at=now_ts(),
                    )
                interrupted.append(job)
            download_jobs[str(job["id"])] = job
        persist_download_jobs_locked()
    for job in interrupted:
        cleanup_interrupted_download(job)


def invalidate_duplicate_jobs() -> None:
    """Prevent a completed background result from replaying after the library changes."""
    with duplicate_jobs_lock:
        for job in duplicate_jobs.values():
            job["stale"] = True


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS roots (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS custom_groups (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS comic_tags (
                comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (comic_id, tag_id)
            );
            CREATE TABLE IF NOT EXISTS comic_groups (
                comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
                group_id INTEGER NOT NULL REFERENCES custom_groups(id) ON DELETE CASCADE,
                PRIMARY KEY (comic_id, group_id)
            );
            CREATE TABLE IF NOT EXISTS duplicate_reviews (
                left_comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
                right_comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
                verdict TEXT NOT NULL CHECK(verdict IN ('duplicate','not_duplicate')),
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (left_comic_id, right_comic_id),
                CHECK(left_comic_id < right_comic_id)
            );
            CREATE TABLE IF NOT EXISTS comics (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0,
                cover_name TEXT,
                author TEXT NOT NULL DEFAULT '',
                author_source TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT '',
                title_guess TEXT NOT NULL DEFAULT '',
                volume_guess TEXT NOT NULL DEFAULT '',
                sha256 TEXT,
                status TEXT NOT NULL DEFAULT 'available',
                last_seen INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS archive_images (
                comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (comic_id, position),
                UNIQUE (comic_id, name)
            );
            CREATE TABLE IF NOT EXISTS scan_issues (
                id INTEGER PRIMARY KEY,
                root_path TEXT NOT NULL,
                path TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                error TEXT NOT NULL,
                detected_at INTEGER NOT NULL,
                UNIQUE(root_path, path, kind)
            );
            CREATE TABLE IF NOT EXISTS similarity_fingerprints (
                comic_id INTEGER PRIMARY KEY REFERENCES comics(id) ON DELETE CASCADE,
                size_bytes INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                algorithm_version INTEGER NOT NULL,
                sample_hashes TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS similarity_candidates (
                left_comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
                right_comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
                score INTEGER NOT NULL,
                median_distance INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (left_comic_id, right_comic_id),
                CHECK(left_comic_id < right_comic_id)
            );
            CREATE INDEX IF NOT EXISTS idx_comics_author ON comics(author);
            CREATE INDEX IF NOT EXISTS idx_comics_group ON comics(group_name);
            CREATE INDEX IF NOT EXISTS idx_comics_seen ON comics(last_seen);
            """
        )
        comic_columns = {row["name"] for row in conn.execute("PRAGMA table_info(comics)")}
        if "author_source" not in comic_columns:
            conn.execute("ALTER TABLE comics ADD COLUMN author_source TEXT NOT NULL DEFAULT ''")
        # Older rows did not record whether the author was inferred or edited.
        # Preserve them without making an unsupported claim about their source.
        conn.execute(
            "UPDATE comics SET author_source='legacy' WHERE author<>'' AND author_source=''"
        )


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def invalidate_similarity(conn: sqlite3.Connection, comic_id: int, *, clear_sha256: bool = False) -> None:
    """Discard only derived data that depends on one changed comic."""
    conn.execute("DELETE FROM similarity_fingerprints WHERE comic_id=?", (comic_id,))
    conn.execute(
        "DELETE FROM similarity_candidates WHERE left_comic_id=? OR right_comic_id=?",
        (comic_id, comic_id),
    )
    if clear_sha256:
        conn.execute("UPDATE comics SET sha256=NULL WHERE id=?", (comic_id,))


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS and not name.endswith("/")


def natural_sort_key(name: str) -> list[tuple[int, Any]]:
    normalized = name.replace("\\", "/")
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", normalized)
        if part
    ]


def parse_filename(path: Path) -> tuple[str, str]:
    stem = path.stem
    volume = ""
    match = re.search(r"(?:vol\.?|v|第)?\s*(\d{1,4})(?:\s*(?:卷|集|話|话))?", stem, re.I)
    if match:
        volume = match.group(1)
    title = re.sub(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】", " ", stem)
    title = re.sub(r"\s+", " ", title).strip() or stem
    return title, volume


def parse_author_guess(path: Path) -> str:
    """Conservative filename hint; metadata remains user-editable and never renames files."""
    blocks = re.findall(r"\[([^\[\]]+)\]", path.stem)
    noise = re.compile(r"翻[譯译]|漢化|汉化|中国|DL版|digital|無修|无修", re.I)
    for block in blocks:
        inner = re.findall(r"\(([^()]*)\)", block)
        candidate = (inner[-1] if inner else block).strip()
        if candidate and len(candidate) <= 80 and not noise.search(candidate):
            return candidate
    return ""


def normalize_author_label(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s・･._\-―—~～]+", "", value)


def split_author_labels(value: str) -> list[str]:
    return [label.strip() for label in re.split(r"[,，、]+", value) if label.strip()]


def consolidate_obvious_author_aliases() -> dict[str, int]:
    """Merge only deterministic case/width/spacing variants; never fuzzy-merge names."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT author,COUNT(*) count FROM comics WHERE status='available' AND author<>'' GROUP BY author ORDER BY count DESC,author"
        ).fetchall()
        variants: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            variants.setdefault(normalize_author_label(row["author"]), []).append(row)
        merged_labels = updated_comics = 0
        for labels in variants.values():
            if len(labels) < 2:
                continue
            canonical = labels[0]["author"]
            for alias in labels[1:]:
                cursor = conn.execute(
                    "UPDATE comics SET author=? WHERE status='available' AND author=?",
                    (canonical, alias["author"]),
                )
                updated_comics += cursor.rowcount
                merged_labels += 1
    return {"merged_labels": merged_labels, "updated_comics": updated_comics}


def migrate_legacy_groups() -> int:
    with connect() as conn:
        rows = conn.execute("SELECT id,group_name FROM comics WHERE group_name<>''").fetchall()
        migrated = 0
        for row in rows:
            for name in [part.strip() for part in re.split(r"[,，、]+", row["group_name"]) if part.strip()]:
                conn.execute("INSERT OR IGNORE INTO custom_groups(name,created_at) VALUES (?,?)", (name, now_ts()))
                group_id = conn.execute("SELECT id FROM custom_groups WHERE name=?", (name,)).fetchone()["id"]
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO comic_groups(comic_id,group_id) VALUES (?,?)", (row["id"], group_id)
                )
                migrated += cursor.rowcount
    return migrated


def clean_local_path(value: str) -> Path:
    cleaned = value.strip().strip('"').strip("'").strip()
    return Path(cleaned).expanduser().resolve()


def available_archive_names(conn: sqlite3.Connection) -> set[str]:
    """Names that represent usable files now; historical rows never block current work."""
    return {
        Path(row["path"]).name.casefold()
        for row in conn.execute("SELECT path FROM comics WHERE status='available'").fetchall()
        if Path(row["path"]).is_file()
    }


def backfill_author_guesses() -> int:
    changed = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,path FROM comics WHERE status='available' AND (author='' OR author IS NULL)"
        ).fetchall()
        for row in rows:
            guess = parse_author_guess(Path(row["path"]))
            if guess:
                conn.execute(
                    "UPDATE comics SET author=?,author_source='guessed' WHERE id=?",
                    (guess, row["id"]),
                )
                changed += 1
    return changed


def inspect_archive(path: Path) -> tuple[list[tuple[str, int]], str]:
    with zipfile.ZipFile(path, "r") as archive:
        # 掃描只讀取目錄，避免大型書庫每次同步都完整解壓；實際覆蓋時才逐圖嚴格驗證。
        images = [(item.filename, item.file_size) for item in archive.infolist() if is_image_name(item.filename)]
        images.sort(key=lambda item: natural_sort_key(item[0]))
    if not images:
        raise ValueError("壓縮檔內沒有可讀取的圖片")
    return images, images[0][0]


def record_scan_issue(root: Path, path: Path, kind: str, error: Exception | str) -> None:
    message = str(error).strip() or type(error).__name__
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO scan_issues(root_path,path,name,kind,error,detected_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(root_path,path,kind) DO UPDATE SET
                name=excluded.name,error=excluded.error,detected_at=excluded.detected_at
            """,
            (str(root), str(path), path.name or str(path), kind, message, now_ts()),
        )


def validate_archive_integrity(path: Path) -> None:
    """Fully decompress the archive and decode every supported image without changing it."""
    with zipfile.ZipFile(path, "r") as archive:
        image_names = [item.filename for item in archive.infolist() if is_image_name(item.filename)]
        if not image_names:
            raise ValueError("壓縮檔內沒有可讀取的圖片")
        for image_name in image_names:
            try:
                with Image.open(io.BytesIO(archive.read(image_name))) as image:
                    image.load()
            except Exception as exc:
                raise ValueError(f"圖片解碼失敗：{image_name}（{exc}）") from exc


def reindex_image_order() -> dict[str, int]:
    """Refresh only the virtual reading order; never writes to source archives."""
    with connect() as conn:
        comics = [dict(row) for row in conn.execute(
            "SELECT id,path FROM comics WHERE status='available' ORDER BY id"
        )]
    updated = failed = 0
    for comic in comics:
        try:
            images, cover = inspect_archive(Path(comic["path"]))
            with connect() as conn:
                conn.execute(
                    "UPDATE comics SET image_count=?,cover_name=?,error='' WHERE id=?",
                    (len(images), cover, comic["id"]),
                )
                conn.execute("DELETE FROM archive_images WHERE comic_id=?", (comic["id"],))
                conn.executemany(
                    "INSERT INTO archive_images(comic_id,position,name,size_bytes) VALUES (?,?,?,?)",
                    [(comic["id"], index, name, size) for index, (name, size) in enumerate(images)],
                )
            updated += 1
        except Exception:
            failed += 1
    return {"updated": updated, "failed": failed}


def scan_directory(root: Path) -> dict[str, int]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("資料夾不存在或無法讀取")
    scan_stamp = now_ts()
    found = updated = failed = skipped = 0
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO roots(path, created_at) VALUES (?, ?)", (str(root), scan_stamp))
        conn.execute("DELETE FROM scan_issues WHERE root_path=?", (str(root),))
    for path in root.rglob("*"):
        if path.suffix.lower() not in ARCHIVE_EXTENSIONS:
            continue
        try:
            is_file = path.is_file()
        except OSError as exc:
            skipped += 1
            record_scan_issue(root, path, "filesystem", exc)
            continue
        if not is_file:
            continue
        found += 1
        try:
            stat = path.stat()
            with connect() as conn:
                old = conn.execute(
                    "SELECT id, size_bytes, modified_at, status FROM comics WHERE path=?", (str(path),)
                ).fetchone()
                unchanged = old and old["size_bytes"] == stat.st_size and old["modified_at"] == stat.st_mtime
                if unchanged:
                    conn.execute("UPDATE comics SET last_seen=?, status='available', error='' WHERE id=?", (scan_stamp, old["id"]))
                    if old["status"] != "available":
                        invalidate_similarity(conn, old["id"])
                    continue
            images, cover = inspect_archive(path)
            title, volume = parse_filename(path)
            author_guess = parse_author_guess(path)
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO comics(path,name,extension,size_bytes,modified_at,image_count,cover_name,
                                       author,author_source,title_guess,volume_guess,status,last_seen,error)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'available',?,'')
                    ON CONFLICT(path) DO UPDATE SET name=excluded.name,extension=excluded.extension,
                        size_bytes=excluded.size_bytes,modified_at=excluded.modified_at,
                        image_count=excluded.image_count,cover_name=excluded.cover_name,
                        author=CASE WHEN comics.author='' THEN excluded.author ELSE comics.author END,
                        author_source=CASE WHEN comics.author='' THEN excluded.author_source ELSE comics.author_source END,
                        title_guess=excluded.title_guess,volume_guess=excluded.volume_guess,
                        status='available',last_seen=excluded.last_seen,error=''
                    """,
                    (str(path), path.name, path.suffix.lower(), stat.st_size, stat.st_mtime,
                     len(images), cover, author_guess, "guessed" if author_guess else "", title, volume, scan_stamp),
                )
                comic_id = conn.execute("SELECT id FROM comics WHERE path=?", (str(path),)).fetchone()["id"]
                invalidate_similarity(conn, comic_id, clear_sha256=True)
                conn.execute("DELETE FROM archive_images WHERE comic_id=?", (comic_id,))
                conn.executemany(
                    "INSERT INTO archive_images(comic_id,position,name,size_bytes) VALUES (?,?,?,?)",
                    [(comic_id, i, name, size) for i, (name, size) in enumerate(images)],
                )
            updated += 1
        except Exception as exc:
            failed += 1
            try:
                stat = path.stat()
            except OSError as stat_exc:
                skipped += 1
                record_scan_issue(root, path, "filesystem", stat_exc)
                continue
            title, volume = parse_filename(path)
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO comics(path,name,extension,size_bytes,modified_at,title_guess,volume_guess,
                                       status,last_seen,error)
                    VALUES (?,?,?,?,?,?,?,'error',?,?)
                    ON CONFLICT(path) DO UPDATE SET status='error',last_seen=excluded.last_seen,error=excluded.error
                    """,
                    (str(path), path.name, path.suffix.lower(), stat.st_size, stat.st_mtime,
                     title, volume, scan_stamp, str(exc)),
                )
                comic_id = conn.execute("SELECT id FROM comics WHERE path=?", (str(path),)).fetchone()["id"]
                invalidate_similarity(conn, comic_id, clear_sha256=True)
    prefix = str(root).rstrip("\\/") + os.sep
    with connect() as conn:
        missing_ids = [row[0] for row in conn.execute(
            "SELECT id FROM comics WHERE status='available' AND (path=? OR path LIKE ?) AND last_seen<>?",
            (str(root), prefix + "%", scan_stamp),
        )]
        conn.execute(
            "UPDATE comics SET status='missing' WHERE (path=? OR path LIKE ?) AND last_seen<>?",
            (str(root), prefix + "%", scan_stamp),
        )
        for comic_id in missing_ids:
            invalidate_similarity(conn, comic_id)
    invalidate_duplicate_jobs()
    return {"found": found, "updated": updated, "failed": failed, "skipped": skipped}


def get_comic(comic_id: int) -> sqlite3.Row:
    with connect() as conn:
        row = conn.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到這個檔案")
    return row


def safe_zip_name(name: str) -> str:
    normalized = name.replace("\\", "/").lstrip("/")
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError(f"不安全的內部檔名：{name}")
    return normalized


def validate_zip_images(path: Path, expected: list[str]) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        actual = [item.filename for item in archive.infolist() if is_image_name(item.filename)]
        if actual != expected:
            raise ValueError("驗證失敗：圖片數量或順序不一致")
        for name in expected:
            with archive.open(name) as stream:
                image = Image.open(stream)
                image.verify()


def rebuild_archive(path: Path, items: list[dict[str, Any]]) -> None:
    if len({item["source"] for item in items}) != len(items):
        raise ValueError("來源圖片清單包含重複項目")
    targets = [safe_zip_name(item["name"]) for item in items if not item.get("deleted")]
    if len(set(targets)) != len(targets):
        raise ValueError("圖片新名稱不可重複")
    token = uuid.uuid4().hex
    temp_path = path.with_name(f".{path.name}.comic-manager-{token}.tmp")
    old_path = path.with_name(f".{path.name}.comic-manager-{token}.old")
    expected_sources = {item["source"] for item in items}
    try:
        with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as target:
            available = {entry.filename for entry in source.infolist()}
            if not expected_sources.issubset(available):
                raise ValueError("原始壓縮檔已在外部變更，請重新掃描")
            for entry in source.infolist():
                if entry.filename not in expected_sources:
                    target.writestr(entry, source.read(entry.filename))
            for item in items:
                if item.get("deleted"):
                    continue
                target.writestr(safe_zip_name(item["name"]), source.read(item["source"]))
        validate_zip_images(temp_path, targets)
        os.replace(path, old_path)
        try:
            os.replace(temp_path, path)
            validate_zip_images(path, targets)
        except Exception:
            if path.exists():
                path.unlink()
            os.replace(old_path, path)
            raise
        old_path.unlink()
    finally:
        if temp_path.exists():
            temp_path.unlink()
        if old_path.exists() and path.exists():
            old_path.unlink()


def normalize_wnacg_album_url(value: str) -> tuple[str, str, str]:
    """Return a safe album URL, its origin and numeric album id."""
    raw = value.strip()
    if not raw:
        raise ValueError("請貼上漫畫目錄網址")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in WNACG_HOSTS:
        raise ValueError("目前只支援 wnacg.com 或 wnacg.ru 的漫畫網址")
    match = re.search(r"(?:^|-)aid-(\d+)(?:\.html)?$", Path(parsed.path).name)
    if not match:
        raise ValueError("無法從網址辨識漫畫編號")
    aid = match.group(1)
    origin = f"https://{host}"
    return f"{origin}/photos-index-page-1-aid-{aid}.html", origin, aid


def fetch_remote_bytes(url: str, referer: str = "", timeout: int = DOWNLOAD_REQUEST_TIMEOUT) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6",
    }
    if referer:
        headers["Referer"] = referer
    request = UrlRequest(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("來源網站暫時限制下載（HTTP 429：請求太頻繁），請稍後再重新下載") from exc
        raise RuntimeError(f"網站回應 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"連線失敗：{exc}") from exc


def extract_wnacg_album(index_html: str, item_script: str, source_url: str) -> dict[str, Any]:
    heading = re.search(r"<h2[^>]*>(.*?)</h2>", index_html, re.IGNORECASE | re.DOTALL)
    if not heading:
        raise ValueError("網站頁面中找不到作品名稱")
    title = html.unescape(re.sub(r"<[^>]+>", "", heading.group(1))).strip()
    page_match = re.search(r"頁數\s*[：:]\s*(\d+)\s*P", index_html, re.IGNORECASE)
    if not page_match:
        raise ValueError("網站頁面中找不到漫畫總頁數")
    reported_page_count = int(page_match.group(1))
    urls = re.findall(
        r"[\"'](https?://[^\"']+?\.(?:jpe?g|png|webp|gif|bmp|avif)(?:\?[^\"']*)?)[\"']",
        item_script,
        re.IGNORECASE,
    )
    unique_urls = list(dict.fromkeys(urls))
    if reported_page_count <= 0 or not unique_urls:
        raise ValueError("網站沒有提供可下載的原圖")
    actual_page_count = len(unique_urls)
    # A small number of WNACG albums have stale metadata that differs from the
    # complete reader manifest by exactly one page.  Treat the manifest as
    # authoritative for that known case, while retaining the strict guard for
    # larger mismatches so genuinely incomplete albums are still rejected.
    if abs(actual_page_count - reported_page_count) > 1:
        raise ValueError(
            f"頁數驗證失敗：網站標示 {reported_page_count} 頁，"
            f"但實際原圖清單有 {actual_page_count} 張"
        )
    return {
        "title": title,
        "page_count": actual_page_count,
        "reported_page_count": reported_page_count,
        "page_count_adjusted": actual_page_count != reported_page_count,
        "image_urls": unique_urls,
        "source_url": source_url,
    }


def inspect_wnacg_album(value: str) -> dict[str, Any]:
    index_url, origin, aid = normalize_wnacg_album_url(value)
    index_bytes = fetch_remote_bytes(index_url)
    index_html = index_bytes.decode("utf-8", errors="replace")
    item_url = f"{origin}/photos-item-aid-{aid}.html"
    item_bytes = fetch_remote_bytes(item_url, referer=index_url)
    item_script = item_bytes.decode("utf-8", errors="replace")
    album = extract_wnacg_album(index_html, item_script, index_url)
    album["aid"] = aid
    return album


def safe_download_stem(title: str) -> str:
    stem = INVALID_WINDOWS_NAME.sub("_", unicodedata.normalize("NFC", title)).strip(" .")
    if not stem:
        stem = "未命名漫畫"
    if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        stem = f"_{stem}"
    return stem[:180].rstrip(" .")


def public_download_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def update_download_job(job_id: str, *, persist: bool = True, **values: Any) -> None:
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if job:
            job.update(values)
            if persist:
                persist_download_jobs_locked()


def validate_downloaded_image(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("收到空白圖片")
    try:
        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        raise RuntimeError("下載內容不是有效圖片") from exc


def download_one_page(
    job_id: str, page_number: int, image_url: str, output_path: Path,
    referer: str, deadline: float, cancel_event: threading.Event,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        if cancel_event.is_set():
            raise RuntimeError("下載已取消")
        if time.monotonic() >= deadline:
            raise TimeoutError("整批下載時間已超過 10 分鐘")
        update_download_job(
            job_id, persist=False,
            message=f"正在下載第 {page_number} 頁（第 {attempt}/{DOWNLOAD_RETRIES} 次）",
        )
        partial_path = output_path.with_suffix(output_path.suffix + ".part")
        try:
            data = fetch_remote_bytes(image_url, referer=referer)
            if len(data) > DOWNLOAD_MAX_IMAGE_BYTES:
                raise RuntimeError("單張圖片超過 100 MB 安全上限")
            partial_path.write_bytes(data)
            validate_downloaded_image(partial_path)
            os.replace(partial_path, output_path)
            return
        except Exception as exc:
            last_error = exc
            partial_path.unlink(missing_ok=True)
            if attempt < DOWNLOAD_RETRIES:
                # Rate limits last longer than ordinary transient failures. Stagger
                # workers too, so every page does not retry at the same instant.
                if "HTTP 429" in str(exc):
                    time.sleep((5 * attempt) + (page_number % 3))
                else:
                    time.sleep(min(attempt, 2))
    raise RuntimeError(f"第 {page_number} 頁重試 {DOWNLOAD_RETRIES} 次仍失敗：{last_error}")


def create_download_archive(temp_archive: Path, page_files: list[Path]) -> None:
    expected = [path.name for path in page_files]
    with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for page_path in page_files:
            archive.write(page_path, page_path.name)
    with zipfile.ZipFile(temp_archive) as archive:
        actual = [item.filename for item in archive.infolist() if is_image_name(item.filename)]
        if actual != expected:
            raise RuntimeError("壓縮檔內的圖片順序驗證失敗")
        bad_file = archive.testzip()
        if bad_file:
            raise RuntimeError(f"壓縮檔驗證失敗：{bad_file}")


def run_download_job(job_id: str, url: str, root_path: Path) -> None:
    temp_dir = root_path / f".comic-download-{job_id}"
    temp_archive: Path | None = None
    cancel_event = download_jobs[job_id]["_cancel_event"]
    try:
        update_download_job(job_id, status="running", message="正在讀取作品資料")
        album = inspect_wnacg_album(url)
        total = album["page_count"]
        stem = safe_download_stem(album["title"])
        target = root_path / f"{stem}.zip"
        if target.exists():
            raise FileExistsError(f"目的資料夾已存在同名檔案：{target.name}")
        temp_dir.mkdir(parents=False, exist_ok=False)
        width = max(3, len(str(total)))
        page_files = []
        for number, image_url in enumerate(album["image_urls"], start=1):
            extension = Path(urlparse(image_url).path).suffix.lower()
            if extension not in IMAGE_EXTENSIONS:
                extension = ".jpg"
            page_files.append(temp_dir / f"{number:0{width}d}{extension}")
        update_download_job(
            job_id, title=album["title"], archive_name=f"{stem}.zip", total=total, completed=0,
            message=f"準備下載 {total} 頁",
        )
        deadline = time.monotonic() + DOWNLOAD_TOTAL_TIMEOUT
        first_error: tuple[int, Exception] | None = None
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="comic-page") as executor:
            futures = {
                executor.submit(
                    download_one_page, job_id, number, image_url, page_files[number - 1],
                    album["source_url"], deadline, cancel_event,
                ): number
                for number, image_url in enumerate(album["image_urls"], start=1)
            }
            for future in as_completed(futures):
                number = futures[future]
                try:
                    future.result()
                    with download_jobs_lock:
                        job = download_jobs[job_id]
                        job["completed"] += 1
                        job["message"] = f"已下載 {job['completed']} / {total} 頁"
                except Exception as exc:
                    if first_error is None:
                        first_error = (number, exc)
                        cancel_event.set()
                    for pending in futures:
                        pending.cancel()
        with download_jobs_lock:
            user_cancelled = bool(download_jobs[job_id].get("_user_cancelled"))
        if user_cancelled:
            raise RuntimeError("下載已由使用者取消")
        if first_error:
            page, error = first_error
            raise RuntimeError(f"第 {page} 頁下載失敗，已取消整批下載：{error}")
        if cancel_event.is_set():
            raise RuntimeError("下載已由使用者取消")
        if time.monotonic() >= deadline:
            raise TimeoutError("整批下載時間已超過 10 分鐘，已自動取消")
        missing = [index for index, path in enumerate(page_files, start=1) if not path.is_file()]
        if missing:
            raise RuntimeError(f"缺少第 {', '.join(map(str, missing))} 頁，未建立壓縮檔")
        update_download_job(job_id, status="packing", message="全部圖片齊全，正在依頁碼建立 ZIP")
        temp_archive = root_path / f".{stem}.{job_id}.partial"
        create_download_archive(temp_archive, page_files)
        if target.exists():
            raise FileExistsError(f"建立期間出現同名檔案，為避免覆蓋已取消：{target.name}")
        os.replace(temp_archive, target)
        scan_directory(root_path)
        update_download_job(
            job_id, status="completed", completed=total, archive_path=str(target),
            message=f"下載完成：{target.name}", finished_at=now_ts(),
        )
    except Exception as exc:
        status = "cancelled" if cancel_event.is_set() and "使用者取消" in str(exc) else "failed"
        update_download_job(
            job_id, status=status, error=str(exc), message=str(exc), finished_at=now_ts(),
        )
    finally:
        if temp_archive and temp_archive.exists():
            temp_archive.unlink(missing_ok=True)
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def run_queued_download_job(job_id: str, url: str, root_path: Path) -> None:
    """Limit whole-album concurrency while still accepting a large batch at once."""
    with download_album_slots:
        with download_jobs_lock:
            job = download_jobs.get(job_id)
            if not job or job["_cancel_event"].is_set():
                if job:
                    job.update(
                        status="cancelled", message="下載已由使用者取消",
                        finished_at=now_ts(),
                    )
                    persist_download_jobs_locked()
                return
        run_download_job(job_id, url, root_path)


def launch_download_job(job_id: str) -> None:
    """Start a persisted job using its saved URL and destination."""
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            return
        url = str(job.get("request_url") or job.get("source_url") or "")
        root_path = Path(str(job.get("root_path") or ""))
    thread = threading.Thread(
        target=run_queued_download_job,
        args=(job_id, url, root_path),
        name=f"web-download-{job_id[:8]}",
        daemon=True,
    )
    thread.start()


def retry_download_job(job_id: str, include_cancelled: bool = False) -> dict[str, Any]:
    """Reset one failed item and download the complete item again."""
    allowed = {"failed", "cancelled"} if include_cancelled else {"failed"}
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job.get("status") not in allowed:
            return public_download_job(job)
        cleanup_interrupted_download(job)
        job["status"] = "queued"
        job["completed"] = 0
        job["message"] = "等待重新下載"
        job["error"] = ""
        job["archive_path"] = ""
        job["finished_at"] = None
        job["retry_count"] = int(job.get("retry_count", 0)) + 1
        job["cancel_requested"] = False
        job["_cancel_event"] = threading.Event()
        job["_user_cancelled"] = False
        persist_download_jobs_locked()
        result = public_download_job(job)
    launch_download_job(job_id)
    return result


class RootRequest(BaseModel):
    path: str


class ScanRequest(BaseModel):
    path: str | None = None


class IntegrityCheckRequest(BaseModel):
    path: str | None = None


class MetadataRequest(BaseModel):
    author: str = Field(max_length=200)
    group_name: str = Field(max_length=200)


class GroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TagRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ComicTagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=50)


class BatchTagsRequest(BaseModel):
    comic_ids: list[int] = Field(min_length=1, max_length=1000)
    tags: list[str] = Field(min_length=1, max_length=50)


class ComicGroupsRequest(BaseModel):
    groups: list[str] = Field(default_factory=list, max_length=50)


class BatchGroupsRequest(BaseModel):
    comic_ids: list[int] = Field(min_length=1, max_length=1000)
    groups: list[str] = Field(min_length=1, max_length=50)


class BatchMetadataRequest(BaseModel):
    comic_ids: list[int] = Field(min_length=1, max_length=1000)
    author: str | None = Field(default=None, max_length=200)
    group_name: str | None = Field(default=None, max_length=200)


class DuplicateReviewRequest(BaseModel):
    comic_ids: list[int] = Field(min_length=2, max_length=100)
    verdict: str = Field(pattern="^(duplicate|not_duplicate)$")


class SimilarGroupRequest(BaseModel):
    comic_ids: list[int] = Field(min_length=2, max_length=100)


class EditItem(BaseModel):
    source: str
    name: str
    deleted: bool = False


class ApplyRequest(BaseModel):
    items: list[EditItem]
    confirmation: str


class FileActionRequest(BaseModel):
    value: str = ""
    confirmation: str


class WebDownloadPreviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=1000)


class WebDownloadStartRequest(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    root_path: str = Field(min_length=1, max_length=1000)


class WebDownloadBatchPreviewRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    root_path: str = Field(min_length=1, max_length=1000)


@app.on_event("startup")
def startup() -> None:
    init_db()
    restore_download_jobs()
    backfill_author_guesses()
    consolidate_obvious_author_aliases()
    migrate_legacy_groups()


@app.get("/api/roots")
def roots() -> list[dict[str, Any]]:
    with connect() as conn:
        total_count = conn.execute("SELECT COUNT(*) FROM comics WHERE status='available'").fetchone()[0]
        result = []
        for row in conn.execute("SELECT * FROM roots ORDER BY path"):
            item = dict(row)
            prefix = item["path"].rstrip("\\/") + os.sep
            item["comic_count"] = conn.execute(
                "SELECT COUNT(*) FROM comics WHERE status='available' AND (path=? OR instr(path,?)=1)",
                (item["path"], prefix),
            ).fetchone()[0]
            item["total_count"] = total_count
            result.append(item)
        return result


@app.post("/api/web-download/preview")
def preview_web_download(request: WebDownloadPreviewRequest) -> dict[str, Any]:
    try:
        album = inspect_wnacg_album(request.url)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "title": album["title"],
        "page_count": album["page_count"],
        "reported_page_count": album.get("reported_page_count", album["page_count"]),
        "page_count_adjusted": album.get("page_count_adjusted", False),
        "archive_name": f"{safe_download_stem(album['title'])}.zip",
        "source_url": album["source_url"],
    }


@app.post("/api/web-download/batch-preview")
def preview_web_download_batch(request: WebDownloadBatchPreviewRequest) -> dict[str, Any]:
    try:
        root_path = clean_local_path(request.root_path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not root_path.is_dir():
        raise HTTPException(400, "下載位置不存在或無法讀取")
    with connect() as conn:
        registered = conn.execute("SELECT 1 FROM roots WHERE path=?", (str(root_path),)).fetchone()
        known_names = available_archive_names(conn)
    with download_jobs_lock:
        active_sources = {
            str(job.get("source_url", "")).casefold()
            for job in download_jobs.values()
            if job.get("status") not in {"completed", "failed", "cancelled"} and job.get("source_url")
        }
        known_names.update(
            str(job.get("archive_name", "")).casefold()
            for job in download_jobs.values()
            if job.get("status") not in {"completed", "failed", "cancelled"} and job.get("archive_name")
        )
    if not registered:
        raise HTTPException(400, "下載位置必須是已加入書庫的掃描資料夾")

    raw_urls = [url.strip() for url in request.urls if url.strip()]
    if not raw_urls:
        raise HTTPException(400, "請至少貼上一個漫畫網址")

    def inspect_one(entry: tuple[int, str]) -> tuple[int, dict[str, Any]]:
        index, url = entry
        try:
            album = inspect_wnacg_album(url)
            archive_name = f"{safe_download_stem(album['title'])}.zip"
            return index, {
                "url": url,
                "source_url": album["source_url"],
                "title": album["title"],
                "page_count": album["page_count"],
                "reported_page_count": album.get("reported_page_count", album["page_count"]),
                "page_count_adjusted": album.get("page_count_adjusted", False),
                "archive_name": archive_name,
                "status": "ready",
                "reason": "",
            }
        except (ValueError, RuntimeError) as exc:
            return index, {
                "url": url, "source_url": "", "title": url,
                "page_count": 0, "archive_name": "", "status": "error",
                "reason": str(exc),
            }

    inspected: list[dict[str, Any] | None] = [None] * len(raw_urls)
    with ThreadPoolExecutor(
        max_workers=min(DOWNLOAD_BATCH_PREVIEW_WORKERS, len(raw_urls)),
        thread_name_prefix="comic-preview",
    ) as executor:
        for future in as_completed(executor.submit(inspect_one, item) for item in enumerate(raw_urls)):
            index, item = future.result()
            inspected[index] = item

    seen_sources: set[str] = set()
    seen_names: set[str] = set()
    for item in inspected:
        if not item or item["status"] != "ready":
            continue
        source_key = item["source_url"].casefold()
        name_key = item["archive_name"].casefold()
        if source_key in seen_sources or source_key in active_sources or name_key in seen_names:
            item["status"] = "paused"
            item["reason"] = "本批次中有重複網址或同名作品"
        elif name_key in known_names or (root_path / item["archive_name"]).exists():
            item["status"] = "paused"
            item["reason"] = "書庫中已存在同名作品"
        else:
            seen_sources.add(source_key)
            seen_names.add(name_key)

    items = [item for item in inspected if item is not None]
    return {
        "items": items,
        "ready_count": sum(item["status"] == "ready" for item in items),
        "paused_count": sum(item["status"] == "paused" for item in items),
        "error_count": sum(item["status"] == "error" for item in items),
    }


@app.post("/api/web-download/start")
def start_web_download(request: WebDownloadStartRequest) -> dict[str, Any]:
    try:
        source_url, _, _ = normalize_wnacg_album_url(request.url)
        root_path = clean_local_path(request.root_path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not root_path.is_dir():
        raise HTTPException(400, "下載位置不存在或無法讀取")
    with connect() as conn:
        registered = conn.execute("SELECT 1 FROM roots WHERE path=?", (str(root_path),)).fetchone()
    if not registered:
        raise HTTPException(400, "下載位置必須是已加入書庫的掃描資料夾")
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "title": "",
        "total": 0,
        "completed": 0,
        "message": "等待開始",
        "error": "",
        "archive_path": "",
        "archive_name": "",
        "source_url": source_url,
        "request_url": request.url,
        "root_path": str(root_path),
        "created_at": now_ts(),
        "finished_at": None,
        "retry_count": 0,
        "cancel_requested": False,
        "_cancel_event": threading.Event(),
        "_user_cancelled": False,
    }
    with download_jobs_lock:
        download_jobs[job_id] = job
        completed_ids = [
            key for key, value in download_jobs.items()
            if value.get("status") in {"completed", "failed", "cancelled"}
        ]
        for old_id in completed_ids[:-20]:
            download_jobs.pop(old_id, None)
        persist_download_jobs_locked()
    thread = threading.Thread(
        target=run_queued_download_job, args=(job_id, request.url, root_path),
        name=f"web-download-{job_id[:8]}", daemon=True,
    )
    thread.start()
    return public_download_job(job)


@app.get("/api/web-download")
def active_web_downloads() -> list[dict[str, Any]]:
    with download_jobs_lock:
        return [
            public_download_job(job)
            for job in download_jobs.values()
            if job.get("status") != "completed"
        ]


@app.post("/api/web-download/retry-failed")
def retry_failed_web_downloads() -> list[dict[str, Any]]:
    with download_jobs_lock:
        job_ids = [
            job_id for job_id, job in download_jobs.items()
            if job.get("status") == "failed"
        ]
    return [retry_download_job(job_id) for job_id in job_ids]


@app.get("/api/web-download/{job_id}")
def web_download_status(job_id: str) -> dict[str, Any]:
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "找不到這筆下載工作")
        return public_download_job(job)


@app.post("/api/web-download/{job_id}/retry")
def retry_web_download(job_id: str) -> dict[str, Any]:
    try:
        return retry_download_job(job_id, include_cancelled=True)
    except KeyError as exc:
        raise HTTPException(404, "找不到這筆下載工作") from exc


@app.post("/api/web-download/{job_id}/cancel")
def cancel_web_download(job_id: str) -> dict[str, Any]:
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "找不到這筆下載工作")
        if job["status"] in {"completed", "failed", "cancelled"}:
            return public_download_job(job)
        job["_user_cancelled"] = True
        job["cancel_requested"] = True
        job["_cancel_event"].set()
        job["message"] = "正在取消下載，等待目前連線結束…"
        persist_download_jobs_locked()
        return public_download_job(job)


@app.delete("/api/web-download/{job_id}")
def delete_web_download(job_id: str) -> dict[str, bool]:
    """Remove a finished download record without touching a completed archive."""
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "找不到這筆下載工作")
        if job.get("status") not in {"completed", "failed", "cancelled"}:
            raise HTTPException(409, "下載仍在進行中，請先取消後再刪除紀錄")
        removed = download_jobs.pop(job_id)
        persist_download_jobs_locked()
    cleanup_interrupted_download(removed)
    return {"deleted": True}


@app.post("/api/pick-folder")
def pick_folder() -> dict[str, str | bool]:
    """Open the native Windows folder picker from the local-only service."""
    if os.name != "nt":
        raise HTTPException(501, "目前只支援 Windows 資料夾選擇器")
    script = APP_DIR / "folder_picker.ps1"
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                "-File", str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(408, "資料夾選擇逾時") from exc
    if result.returncode != 0:
        raise HTTPException(500, result.stderr.strip() or "無法開啟資料夾選擇視窗")
    selected = result.stdout.strip().lstrip("\ufeff")
    if not selected:
        return {"selected": False, "path": ""}
    path = Path(selected).resolve()
    if not path.is_dir():
        raise HTTPException(400, "選取的資料夾不存在或無法讀取")
    return {"selected": True, "path": str(path)}


@app.post("/api/roots")
def add_root(request: RootRequest) -> dict[str, Any]:
    path = clean_local_path(request.path)
    if not path.is_dir():
        raise HTTPException(400, "資料夾不存在或無法讀取")
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO roots(path,created_at) VALUES (?,?)", (str(path), now_ts()))
    return {"path": str(path)}


@app.get("/api/groups")
def list_groups() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(
            """
            SELECT custom_groups.id,custom_groups.name,COUNT(CASE WHEN comics.status='available' THEN 1 END) AS comic_count
            FROM custom_groups
            LEFT JOIN comic_groups ON comic_groups.group_id=custom_groups.id
            LEFT JOIN comics ON comics.id=comic_groups.comic_id
            GROUP BY custom_groups.id,custom_groups.name
            ORDER BY custom_groups.name COLLATE NOCASE
            """
        )]


@app.post("/api/groups")
def create_group(request: GroupRequest) -> dict[str, Any]:
    name = request.name.strip()
    if not name:
        raise HTTPException(400, "分組名稱不可空白")
    try:
        with connect() as conn:
            cursor = conn.execute("INSERT INTO custom_groups(name,created_at) VALUES (?,?)", (name, now_ts()))
            group_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "這個分組已經存在") from exc
    return {"id": group_id, "name": name, "comic_count": 0}


def clean_tag_names(values: list[str]) -> list[str]:
    names = []
    seen = set()
    for value in values:
        name = value.strip().lstrip("#").strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        if len(name) > 80:
            raise HTTPException(400, "單一標籤不可超過 80 個字元")
        names.append(name)
        seen.add(key)
    return names


def ensure_tags(conn: sqlite3.Connection, names: list[str]) -> dict[str, int]:
    for name in names:
        conn.execute("INSERT OR IGNORE INTO tags(name,created_at) VALUES (?,?)", (name, now_ts()))
    return {
        row["name"]: row["id"]
        for row in conn.execute(
            f"SELECT id,name FROM tags WHERE name IN ({','.join('?' for _ in names)})", names
        )
    } if names else {}


@app.get("/api/tags")
def list_tags() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(
            """
            SELECT tags.id,tags.name,COUNT(CASE WHEN comics.status='available' THEN 1 END) AS comic_count
            FROM tags
            LEFT JOIN comic_tags ON comic_tags.tag_id=tags.id
            LEFT JOIN comics ON comics.id=comic_tags.comic_id
            GROUP BY tags.id,tags.name
            ORDER BY tags.name COLLATE NOCASE
            """
        )]


@app.post("/api/tags")
def create_tag(request: TagRequest) -> dict[str, Any]:
    names = clean_tag_names([request.name])
    if not names:
        raise HTTPException(400, "標籤名稱不可為空白")
    name = names[0]
    try:
        with connect() as conn:
            cursor = conn.execute("INSERT INTO tags(name,created_at) VALUES (?,?)", (name, now_ts()))
            tag_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "這個標籤已經存在") from exc
    return {"id": tag_id, "name": name, "comic_count": 0}


@app.patch("/api/comics/batch-metadata")
def batch_update_metadata(request: BatchMetadataRequest) -> dict[str, int]:
    if request.author is None and request.group_name is None:
        raise HTTPException(400, "沒有提供要更新的內容")
    placeholders = ",".join("?" for _ in request.comic_ids)
    fields, params = [], []
    if request.author is not None:
        author = request.author.strip()
        fields.extend(["author=?", "author_source=?"])
        params.extend([author, "manual" if author else ""])
    if request.group_name is not None:
        group_name = request.group_name.strip()
        if group_name:
            with connect() as conn:
                conn.execute("INSERT OR IGNORE INTO custom_groups(name,created_at) VALUES (?,?)", (group_name, now_ts()))
        fields.append("group_name=?")
        params.append(group_name)
    params.extend(request.comic_ids)
    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE comics SET {','.join(fields)} WHERE id IN ({placeholders}) AND status='available'",
            params,
        )
    return {"updated": cursor.rowcount}


@app.patch("/api/comics/batch-tags")
def batch_add_tags(request: BatchTagsRequest) -> dict[str, int]:
    names = clean_tag_names(request.tags)
    with connect() as conn:
        tag_ids = ensure_tags(conn, names).values()
        inserted = 0
        for comic_id in set(request.comic_ids):
            for tag_id in tag_ids:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO comic_tags(comic_id,tag_id) VALUES (?,?)", (comic_id, tag_id)
                )
                inserted += cursor.rowcount
    return {"updated_comics": len(set(request.comic_ids)), "inserted_links": inserted}


@app.patch("/api/comics/batch-groups")
def batch_add_groups(request: BatchGroupsRequest) -> dict[str, int]:
    names = clean_tag_names(request.groups)
    with connect() as conn:
        for name in names:
            conn.execute("INSERT OR IGNORE INTO custom_groups(name,created_at) VALUES (?,?)", (name, now_ts()))
        group_ids = [
            row["id"] for row in conn.execute(
                f"SELECT id FROM custom_groups WHERE name IN ({','.join('?' for _ in names)})", names
            )
        ] if names else []
        inserted = 0
        for comic_id in set(request.comic_ids):
            for group_id in group_ids:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO comic_groups(comic_id,group_id) VALUES (?,?)", (comic_id, group_id)
                )
                inserted += cursor.rowcount
    return {"updated_comics": len(set(request.comic_ids)), "inserted_links": inserted}


@app.post("/api/duplicates/review")
def save_duplicate_review(request: DuplicateReviewRequest) -> dict[str, int | str]:
    comic_ids = sorted(set(request.comic_ids))
    if len(comic_ids) < 2:
        raise HTTPException(400, "至少需要兩個不同檔案")
    pairs = [
        (comic_ids[left], comic_ids[right], request.verdict, now_ts())
        for left in range(len(comic_ids))
        for right in range(left + 1, len(comic_ids))
    ]
    with connect() as conn:
        existing = conn.execute(
            f"SELECT COUNT(*) FROM comics WHERE status='available' AND id IN ({','.join('?' for _ in comic_ids)})",
            comic_ids,
        ).fetchone()[0]
        if existing != len(comic_ids):
            raise HTTPException(404, "部分檔案已不存在於索引")
        conn.executemany(
            """
            INSERT INTO duplicate_reviews(left_comic_id,right_comic_id,verdict,updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(left_comic_id,right_comic_id) DO UPDATE SET
                verdict=excluded.verdict,updated_at=excluded.updated_at
            """,
            pairs,
        )
    return {"reviewed_pairs": len(pairs), "verdict": request.verdict}


def duplicate_verdict(comic_ids: list[int]) -> str:
    comic_ids = sorted(set(comic_ids))
    if len(comic_ids) < 2:
        return ""
    with connect() as conn:
        verdicts = []
        for left in range(len(comic_ids)):
            for right in range(left + 1, len(comic_ids)):
                row = conn.execute(
                    "SELECT verdict FROM duplicate_reviews WHERE left_comic_id=? AND right_comic_id=?",
                    (comic_ids[left], comic_ids[right]),
                ).fetchone()
                if row:
                    verdicts.append(row["verdict"])
    expected = len(comic_ids) * (len(comic_ids) - 1) // 2
    return verdicts[0] if len(verdicts) == expected and len(set(verdicts)) == 1 else ""


@app.post("/api/scan")
def scan(request: ScanRequest) -> dict[str, Any]:
    if request.path:
        paths = [clean_local_path(request.path)]
    else:
        with connect() as conn:
            paths = [Path(row["path"]) for row in conn.execute("SELECT path FROM roots")]
    if not paths:
        raise HTTPException(400, "請先新增掃描資料夾")
    total = {"found": 0, "updated": 0, "failed": 0, "skipped": 0}
    for path in paths:
        try:
            result = scan_directory(path)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        for key in total:
            total[key] += result[key]
    return total


@app.get("/api/issues")
def scan_issues() -> dict[str, Any]:
    with connect() as conn:
        archive_errors = [dict(row) for row in conn.execute(
            """
            SELECT id AS comic_id,path,name,'archive' AS kind,error,last_seen AS detected_at
            FROM comics WHERE status='error'
            ORDER BY last_seen DESC,name COLLATE NOCASE
            """
        )]
        filesystem_errors = [dict(row) for row in conn.execute(
            """
            SELECT id AS issue_id,path,name,kind,error,detected_at
            FROM scan_issues
            ORDER BY detected_at DESC,name COLLATE NOCASE
            """
        )]
    items = archive_errors + filesystem_errors
    items.sort(key=lambda item: (-int(item["detected_at"]), item["name"].casefold()))
    return {
        "items": items,
        "total": len(items),
        "archive_count": len(archive_errors),
        "filesystem_count": len(filesystem_errors),
    }


@app.post("/api/issues/check")
def check_archive_integrity(request: IntegrityCheckRequest) -> dict[str, int]:
    params: list[Any] = []
    clauses = ["status IN ('available','error')"]
    if request.path:
        root = clean_local_path(request.path)
        prefix = str(root).rstrip("\\/") + os.sep
        clauses.append("(path=? OR instr(path,?)=1)")
        params.extend([str(root), prefix])
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            f"SELECT id,path FROM comics WHERE {' AND '.join(clauses)} ORDER BY path COLLATE NOCASE",
            params,
        )]
    checked = failed = 0
    for row in rows:
        checked += 1
        try:
            validate_archive_integrity(Path(row["path"]))
        except Exception as exc:
            failed += 1
            with connect() as conn:
                conn.execute(
                    "UPDATE comics SET status='error',error=?,last_seen=? WHERE id=?",
                    (f"完整性檢查失敗：{exc}", now_ts(), row["id"]),
                )
                invalidate_similarity(conn, row["id"])
        else:
            with connect() as conn:
                conn.execute("UPDATE comics SET status='available',error='' WHERE id=?", (row["id"],))
    return {"checked": checked, "failed": failed, "passed": checked - failed}


@app.post("/api/issues/{issue_id}/delete")
def delete_scan_issue(issue_id: int, request: FileActionRequest) -> dict[str, str]:
    if request.confirmation != "confirm-delete":
        raise HTTPException(400, "尚未確認刪除")
    with connect() as conn:
        row = conn.execute("SELECT * FROM scan_issues WHERE id=?", (issue_id,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到這筆異常紀錄")
    try:
        os.unlink(row["path"])
    except FileNotFoundError:
        status = "already_deleted"
    except OSError as exc:
        raise HTTPException(422, f"Windows 無法刪除這個損毀項目：{exc}") from exc
    else:
        status = "deleted"
    with connect() as conn:
        conn.execute("DELETE FROM scan_issues WHERE id=?", (issue_id,))
    return {"status": status}


@app.get("/api/comics")
def comics(
    q: str = "", author: str = "", group: str = "", status: str = "available",
    root: str = "", tag: str = "",
    sort: str = Query("modified_at", pattern="^(name|author|group_name|size_bytes|modified_at)$"),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if q:
        clauses.append("(name LIKE ? OR path LIKE ? OR author LIKE ? OR group_name LIKE ? OR title_guess LIKE ? OR volume_guess LIKE ? OR EXISTS (SELECT 1 FROM comic_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.comic_id=comics.id AND t.name LIKE ?) OR EXISTS (SELECT 1 FROM comic_groups cg JOIN custom_groups g ON g.id=cg.group_id WHERE cg.comic_id=comics.id AND g.name LIKE ?))")
        params.extend([f"%{q}%"] * 8)
    if author:
        clauses.append(
            "instr(',' || replace(replace(replace(author,'，',','),'、',','),' ','') || ',', ',' || ? || ',') > 0"
        )
        params.append(author.replace(" ", ""))
    if group:
        clauses.append("EXISTS (SELECT 1 FROM comic_groups cg JOIN custom_groups g ON g.id=cg.group_id WHERE cg.comic_id=comics.id AND g.name=?)")
        params.append(group)
    if root:
        root_path = str(Path(root).expanduser().resolve())
        clauses.append("(path=? OR instr(path,?)=1)")
        params.extend([root_path, root_path.rstrip("\\/") + os.sep])
    if tag:
        clauses.append("EXISTS (SELECT 1 FROM comic_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.comic_id=comics.id AND t.name=?)")
        params.append(tag)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(
            f"SELECT * FROM comics {where} ORDER BY {sort} COLLATE NOCASE {direction.upper()}, name COLLATE NOCASE ASC",
            params,
        )]
        tag_map: dict[int, list[str]] = {}
        for tag_row in conn.execute(
            "SELECT comic_tags.comic_id,tags.name FROM comic_tags JOIN tags ON tags.id=comic_tags.tag_id ORDER BY tags.name COLLATE NOCASE"
        ):
            tag_map.setdefault(tag_row["comic_id"], []).append(tag_row["name"])
        for row in rows:
            row["tags"] = tag_map.get(row["id"], [])
        group_map: dict[int, list[str]] = {}
        for group_row in conn.execute(
            "SELECT comic_groups.comic_id,custom_groups.name FROM comic_groups JOIN custom_groups ON custom_groups.id=comic_groups.group_id ORDER BY custom_groups.name COLLATE NOCASE"
        ):
            group_map.setdefault(group_row["comic_id"], []).append(group_row["name"])
        for row in rows:
            row["groups"] = group_map.get(row["id"], [])
        author_values = [row[0] for row in conn.execute(
            "SELECT author FROM comics WHERE status='available' AND author<>''"
        )]
        authors = sorted({label for value in author_values for label in split_author_labels(value)}, key=str.casefold)
        groups = [row[0] for row in conn.execute(
            "SELECT DISTINCT group_name FROM comics WHERE status='available' AND group_name<>'' ORDER BY group_name"
        )]
    return {"items": rows, "authors": authors, "groups": groups}


@app.get("/api/comics/{comic_id}")
def comic_detail(comic_id: int) -> dict[str, Any]:
    row = dict(get_comic(comic_id))
    with connect() as conn:
        row["images"] = [dict(image) for image in conn.execute(
            "SELECT position,name,size_bytes FROM archive_images WHERE comic_id=? ORDER BY position", (comic_id,)
        )]
        row["tags"] = [tag[0] for tag in conn.execute(
            "SELECT tags.name FROM comic_tags JOIN tags ON tags.id=comic_tags.tag_id WHERE comic_tags.comic_id=? ORDER BY tags.name COLLATE NOCASE",
            (comic_id,),
        )]
        row["groups"] = [group[0] for group in conn.execute(
            "SELECT custom_groups.name FROM comic_groups JOIN custom_groups ON custom_groups.id=comic_groups.group_id WHERE comic_groups.comic_id=? ORDER BY custom_groups.name COLLATE NOCASE",
            (comic_id,),
        )]
    return row


@app.put("/api/comics/{comic_id}/tags")
def replace_comic_tags(comic_id: int, request: ComicTagsRequest) -> dict[str, Any]:
    get_comic(comic_id)
    names = clean_tag_names(request.tags)
    with connect() as conn:
        tag_ids = ensure_tags(conn, names)
        conn.execute("DELETE FROM comic_tags WHERE comic_id=?", (comic_id,))
        conn.executemany(
            "INSERT INTO comic_tags(comic_id,tag_id) VALUES (?,?)",
            [(comic_id, tag_id) for tag_id in tag_ids.values()],
        )
    return {"tags": names}


@app.put("/api/comics/{comic_id}/groups")
def replace_comic_groups(comic_id: int, request: ComicGroupsRequest) -> dict[str, Any]:
    get_comic(comic_id)
    names = clean_tag_names(request.groups)
    with connect() as conn:
        for name in names:
            conn.execute("INSERT OR IGNORE INTO custom_groups(name,created_at) VALUES (?,?)", (name, now_ts()))
        group_ids = [
            row["id"] for row in conn.execute(
                f"SELECT id FROM custom_groups WHERE name IN ({','.join('?' for _ in names)})", names
            )
        ] if names else []
        conn.execute("DELETE FROM comic_groups WHERE comic_id=?", (comic_id,))
        conn.executemany(
            "INSERT INTO comic_groups(comic_id,group_id) VALUES (?,?)",
            [(comic_id, group_id) for group_id in group_ids],
        )
        conn.execute("UPDATE comics SET group_name=? WHERE id=?", (names[0] if names else "", comic_id))
    return {"groups": names}


@app.patch("/api/comics/{comic_id}/metadata")
def update_metadata(comic_id: int, request: MetadataRequest) -> dict[str, str]:
    get_comic(comic_id)
    author = request.author.strip()
    with connect() as conn:
        conn.execute("UPDATE comics SET author=?,author_source=?,group_name=? WHERE id=?",
                     (author, "manual" if author else "", request.group_name.strip(), comic_id))
    return {"status": "ok"}


@app.get("/api/comics/{comic_id}/image")
def archive_image(comic_id: int, name: str) -> Response:
    row = get_comic(comic_id)
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM archive_images WHERE comic_id=? AND name=?", (comic_id, name)).fetchone()
    if not exists:
        raise HTTPException(404, "找不到圖片")
    try:
        with zipfile.ZipFile(row["path"], "r") as archive:
            data = archive.read(name)
        image = Image.open(io.BytesIO(data))
        media = Image.MIME.get(image.format, "application/octet-stream")
        return Response(data, media_type=media, headers={"Cache-Control": "private, max-age=3600"})
    except Exception as exc:
        raise HTTPException(422, f"圖片無法讀取：{exc}") from exc


@app.get("/api/comics/{comic_id}/thumbnail")
def archive_thumbnail(comic_id: int, name: str, width: int = Query(160, ge=80, le=360)) -> Response:
    """Return a small local preview so sorting never decodes full-size pages in the browser."""
    row = get_comic(comic_id)
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM archive_images WHERE comic_id=? AND name=?", (comic_id, name)
        ).fetchone()
    if not exists:
        raise HTTPException(404, "找不到圖片")
    try:
        with zipfile.ZipFile(row["path"], "r") as archive:
            data = archive.read(name)
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
            image.thumbnail((width, width * 2), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=76, optimize=True)
        return Response(output.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
    except Exception as exc:
        raise HTTPException(422, f"縮圖無法讀取：{exc}") from exc


@app.post("/api/comics/{comic_id}/apply")
def apply_edits(comic_id: int, request: ApplyRequest) -> dict[str, Any]:
    if request.confirmation != "確認":
        raise HTTPException(400, "確認文字不正確")
    row = get_comic(comic_id)
    try:
        path = Path(row["path"])
        staged_items = [item.model_dump() for item in request.items]
        rebuild_archive(path, staged_items)
        ordered_names = [safe_zip_name(item["name"]) for item in staged_items if not item.get("deleted")]
        with zipfile.ZipFile(path, "r") as archive:
            sizes = {entry.filename: entry.file_size for entry in archive.infolist()}
        images = [(name, sizes[name]) for name in ordered_names]
        cover = ordered_names[0] if ordered_names else ""
        stat = path.stat()
        with connect() as conn:
            conn.execute(
                "UPDATE comics SET size_bytes=?,modified_at=?,image_count=?,cover_name=?,sha256=NULL,last_seen=?,error='',status='available' WHERE id=?",
                (stat.st_size, stat.st_mtime, len(images), cover, now_ts(), comic_id),
            )
            invalidate_similarity(conn, comic_id)
            conn.execute("DELETE FROM archive_images WHERE comic_id=?", (comic_id,))
            conn.executemany(
                "INSERT INTO archive_images(comic_id,position,name,size_bytes) VALUES (?,?,?,?)",
                [(comic_id, i, name, size) for i, (name, size) in enumerate(images)],
            )
    except Exception as exc:
        raise HTTPException(422, f"未覆蓋原始檔：{exc}") from exc
    return {"status": "ok", "images": sum(not item.deleted for item in request.items)}


def validate_new_filename(name: str, extension: str) -> str:
    name = name.strip()
    if not name or name in {".", ".."} or INVALID_WINDOWS_NAME.search(name):
        raise HTTPException(400, "檔名包含 Windows 不允許的字元")
    if Path(name).suffix.lower() not in ARCHIVE_EXTENSIONS:
        name += extension
    return name


@app.post("/api/comics/{comic_id}/rename")
def rename_comic(comic_id: int, request: FileActionRequest) -> dict[str, str]:
    row = get_comic(comic_id)
    if request.confirmation != row["name"]:
        raise HTTPException(400, "請輸入目前完整檔名確認")
    source = Path(row["path"])
    target = source.with_name(validate_new_filename(request.value, row["extension"]))
    if target.exists():
        raise HTTPException(409, "目標檔案已存在")
    source.rename(target)
    with connect() as conn:
        conn.execute("UPDATE comics SET path=?,name=?,modified_at=? WHERE id=?",
                     (str(target), target.name, target.stat().st_mtime, comic_id))
    return {"path": str(target)}


@app.post("/api/comics/{comic_id}/move")
def move_comic(comic_id: int, request: FileActionRequest) -> dict[str, str]:
    row = get_comic(comic_id)
    if request.confirmation != row["name"]:
        raise HTTPException(400, "請輸入目前完整檔名確認")
    destination = Path(request.value).expanduser().resolve()
    if not destination.is_dir():
        raise HTTPException(400, "目的資料夾不存在")
    source = Path(row["path"])
    target = destination / source.name
    if target.exists():
        raise HTTPException(409, "目的地已有同名檔案")
    shutil.move(str(source), str(target))
    with connect() as conn:
        conn.execute("UPDATE comics SET path=?,modified_at=? WHERE id=?", (str(target), target.stat().st_mtime, comic_id))
    return {"path": str(target)}


@app.post("/api/comics/{comic_id}/delete")
def delete_comic(comic_id: int, request: FileActionRequest) -> dict[str, str]:
    row = get_comic(comic_id)
    if request.confirmation != "confirm-delete":
        raise HTTPException(400, "尚未確認刪除")
    path = Path(row["path"])
    already_deleted = not path.exists()
    if not already_deleted:
        path.unlink()
    with connect() as conn:
        conn.execute("UPDATE comics SET status='deleted' WHERE id=?", (comic_id,))
        invalidate_similarity(conn, comic_id)
    invalidate_duplicate_jobs()
    return {"status": "already_deleted" if already_deleted else "deleted"}


@app.post("/api/comics/{comic_id}/open-folder")
def open_folder(comic_id: int) -> dict[str, str]:
    path = Path(get_comic(comic_id)["path"])
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])
    return {"status": "ok"}


@app.post("/api/duplicates/hash")
def calculate_duplicates() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT id,path,size_bytes,sha256 FROM comics WHERE status='available' ORDER BY size_bytes").fetchall()
    existing_rows = []
    missing_ids = []
    for row in rows:
        try:
            exists = Path(row["path"]).is_file()
        except OSError:
            exists = False
        if exists:
            existing_rows.append(row)
        else:
            missing_ids.append(row["id"])
    if missing_ids:
        with connect() as conn:
            conn.executemany("UPDATE comics SET status='missing' WHERE id=?", [(comic_id,) for comic_id in missing_ids])
            for comic_id in missing_ids:
                invalidate_similarity(conn, comic_id)
    rows = existing_rows
    by_size: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_size.setdefault(row["size_bytes"], []).append(row)
    calculated = 0
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        for row in candidates:
            if row["sha256"]:
                continue
            digest = hashlib.sha256()
            with open(row["path"], "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            with connect() as conn:
                conn.execute("UPDATE comics SET sha256=? WHERE id=?", (digest.hexdigest(), row["id"]))
            calculated += 1
    with connect() as conn:
        hashes = [row[0] for row in conn.execute(
            "SELECT sha256 FROM comics WHERE status='available' AND sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1"
        )]
        groups = []
        for digest in hashes:
            files = [dict(row) for row in conn.execute(
                "SELECT id,name,path,image_count,size_bytes,modified_at FROM comics WHERE status='available' AND sha256=? ORDER BY modified_at DESC,id DESC", (digest,)
            )]
            verdict = duplicate_verdict([file["id"] for file in files])
            if verdict == "not_duplicate":
                continue
            groups.append({
                "sha256": digest,
                "files": files,
                "verdict": verdict,
            })
        groups.sort(key=lambda group: group["files"][0]["modified_at"], reverse=True)
    return {"calculated": calculated, "groups": groups}


def public_duplicate_job(job: dict[str, Any]) -> dict[str, Any]:
    if job.get("stale") and job.get("status") in {"completed", "failed"}:
        return {"status": "idle"}
    return {
        key: value for key, value in job.items()
        if not key.startswith("_")
    }


def run_duplicate_job(job_id: str) -> None:
    with duplicate_jobs_lock:
        job = duplicate_jobs.get(job_id)
        if not job:
            return
        job.update(status="running", message="正在背景比對檔案內容…")
    try:
        result = calculate_duplicates()
    except Exception as exc:
        with duplicate_jobs_lock:
            job = duplicate_jobs.get(job_id)
            if job:
                job.update(
                    status="failed", message="重複檢查失敗", error=str(exc),
                    finished_at=now_ts(), acknowledged=False,
                )
    else:
        with duplicate_jobs_lock:
            job = duplicate_jobs.get(job_id)
            if job:
                job.update(
                    status="completed", message="重複檢查完成", result=result,
                    finished_at=now_ts(), acknowledged=False,
                )


@app.post("/api/duplicates/jobs")
def start_duplicate_job() -> dict[str, Any]:
    with duplicate_jobs_lock:
        active = next(
            (job for job in duplicate_jobs.values() if job["status"] in {"queued", "running"}),
            None,
        )
        if active:
            return public_duplicate_job(active)
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "queued",
            "message": "準備背景檢查…",
            "error": "",
            "result": None,
            "created_at": now_ts(),
            "finished_at": None,
            "acknowledged": True,
            "stale": False,
        }
        duplicate_jobs[job_id] = job
        old_ids = [key for key in duplicate_jobs if key != job_id]
        for old_id in old_ids[:-4]:
            duplicate_jobs.pop(old_id, None)
    threading.Thread(
        target=run_duplicate_job, args=(job_id,),
        name=f"duplicate-check-{job_id[:8]}", daemon=True,
    ).start()
    return public_duplicate_job(job)


@app.get("/api/duplicates/jobs/latest")
def latest_duplicate_job() -> dict[str, Any]:
    with duplicate_jobs_lock:
        if not duplicate_jobs:
            return {"status": "idle"}
        job = max(duplicate_jobs.values(), key=lambda item: item["created_at"])
        return public_duplicate_job(job)


@app.get("/api/duplicates/jobs/{job_id}")
def duplicate_job_status(job_id: str) -> dict[str, Any]:
    with duplicate_jobs_lock:
        job = duplicate_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "找不到這次重複檢查")
        return public_duplicate_job(job)


@app.post("/api/duplicates/jobs/{job_id}/acknowledge")
def acknowledge_duplicate_job(job_id: str) -> dict[str, Any]:
    with duplicate_jobs_lock:
        job = duplicate_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "找不到這次重複檢查")
        job["acknowledged"] = True
        return public_duplicate_job(job)


def perceptual_hash(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as image:
        pixels = list(image.convert("L").resize((8, 8), Image.Resampling.LANCZOS).get_flattened_data())
    average = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | int(pixel >= average)
    return value


def sample_archive_hashes(path: Path, names: list[str]) -> list[int]:
    if not names:
        return []
    indexes = sorted({round((len(names) - 1) * fraction / 4) for fraction in range(5)})
    with zipfile.ZipFile(path, "r") as archive:
        return [perceptual_hash(archive.read(names[index])) for index in indexes]


@app.post("/api/duplicates/similar")
def calculate_similar_content() -> dict[str, Any]:
    return calculate_similar_content_for_ids()


@app.post("/api/duplicates/similar-selected")
def calculate_selected_similar_content(request: SimilarGroupRequest) -> dict[str, Any]:
    return calculate_similar_content_for_ids(set(request.comic_ids))


def calculate_similar_content_for_ids(selected_ids: set[int] | None = None) -> dict[str, Any]:
    with connect() as conn:
        comics = [dict(row) for row in conn.execute(
            "SELECT id,name,path,image_count,size_bytes,modified_at FROM comics WHERE status='available' AND image_count>0 ORDER BY id"
        )]
        fingerprints = {
            row["comic_id"]: dict(row)
            for row in conn.execute("SELECT * FROM similarity_fingerprints")
        }
    if selected_ids is not None:
        comics = [comic for comic in comics if comic["id"] in selected_ids]
        if len(comics) != len(selected_ids):
            raise HTTPException(404, "部分檔案已不存在或沒有可比較的圖片")

    stale = [
        comic for comic in comics
        if comic["id"] not in fingerprints
        or fingerprints[comic["id"]]["size_bytes"] != comic["size_bytes"]
        or fingerprints[comic["id"]]["modified_at"] != comic["modified_at"]
        or fingerprints[comic["id"]]["algorithm_version"] != SIMILARITY_ALGORITHM_VERSION
    ]
    errors: list[dict[str, Any]] = []
    for comic in stale:
        try:
            with connect() as conn:
                image_names = [row[0] for row in conn.execute(
                    "SELECT name FROM archive_images WHERE comic_id=? ORDER BY position", (comic["id"],)
                )]
            sample_hashes = sample_archive_hashes(Path(comic["path"]), image_names)
            error = ""
        except Exception as exc:
            sample_hashes = []
            error = str(exc)
            errors.append({"id": comic["id"], "name": comic["name"], "error": error})
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO similarity_fingerprints
                    (comic_id,size_bytes,modified_at,algorithm_version,sample_hashes,error,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(comic_id) DO UPDATE SET
                    size_bytes=excluded.size_bytes,modified_at=excluded.modified_at,
                    algorithm_version=excluded.algorithm_version,sample_hashes=excluded.sample_hashes,
                    error=excluded.error,updated_at=excluded.updated_at
                """,
                (comic["id"], comic["size_bytes"], comic["modified_at"],
                 SIMILARITY_ALGORITHM_VERSION, json.dumps(sample_hashes), error, now_ts()),
            )
            conn.execute(
                "DELETE FROM similarity_candidates WHERE left_comic_id=? OR right_comic_id=?",
                (comic["id"], comic["id"]),
            )

    with connect() as conn:
        fingerprint_rows = [dict(row) for row in conn.execute(
            """
            SELECT f.comic_id,f.sample_hashes,f.error,c.image_count
            FROM similarity_fingerprints f JOIN comics c ON c.id=f.comic_id
            WHERE c.status='available' AND c.image_count>0
              AND f.algorithm_version=?
            """,
            (SIMILARITY_ALGORITHM_VERSION,),
        )]
    if selected_ids is not None:
        fingerprint_rows = [row for row in fingerprint_rows if row["comic_id"] in selected_ids]
    samples = {row["comic_id"]: json.loads(row["sample_hashes"]) for row in fingerprint_rows if not row["error"]}
    page_counts = {row["comic_id"]: row["image_count"] for row in fingerprint_rows}
    stale_ids = {comic["id"] for comic in stale}
    compared: set[tuple[int, int]] = set()
    candidate_rows: list[tuple[int, int, int, int, int]] = []
    for left_id in stale_ids:
        left_hashes = samples.get(left_id)
        if not left_hashes:
            continue
        for right_id, right_hashes in samples.items():
            if left_id == right_id:
                continue
            pair = (min(left_id, right_id), max(left_id, right_id))
            if pair in compared:
                continue
            compared.add(pair)
            if not right_hashes:
                continue
            page_ratio = min(page_counts[left_id], page_counts[right_id]) / max(page_counts[left_id], page_counts[right_id])
            if page_ratio < 0.85 or len(left_hashes) != len(right_hashes):
                continue
            distances = sorted((a ^ b).bit_count() for a, b in zip(left_hashes, right_hashes))
            median_distance = distances[len(distances) // 2]
            if median_distance <= 8:
                confidence = round(100 * page_ratio * (1 - median_distance / 64))
                candidate_rows.append((pair[0], pair[1], confidence, median_distance, now_ts()))
    if candidate_rows:
        with connect() as conn:
            conn.executemany(
                """
                INSERT INTO similarity_candidates(left_comic_id,right_comic_id,score,median_distance,updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(left_comic_id,right_comic_id) DO UPDATE SET
                    score=excluded.score,median_distance=excluded.median_distance,updated_at=excluded.updated_at
                """,
                candidate_rows,
            )

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT sc.score,sc.median_distance,
                   l.id AS left_id,l.name AS left_name,l.path AS left_path,
                   l.image_count AS left_image_count,l.size_bytes AS left_size_bytes,l.modified_at AS left_modified_at,
                   r.id AS right_id,r.name AS right_name,r.path AS right_path,
                   r.image_count AS right_image_count,r.size_bytes AS right_size_bytes,r.modified_at AS right_modified_at
            FROM similarity_candidates sc
            JOIN comics l ON l.id=sc.left_comic_id AND l.status='available'
            JOIN comics r ON r.id=sc.right_comic_id AND r.status='available'
            ORDER BY sc.score DESC,l.name COLLATE NOCASE
            """
        ).fetchall()
    matches = []
    for row in rows:
        if selected_ids is not None and (
            row["left_id"] not in selected_ids or row["right_id"] not in selected_ids
        ):
            continue
        verdict = duplicate_verdict([row["left_id"], row["right_id"]])
        if verdict == "not_duplicate":
            continue
        left_file = {"id": row["left_id"], "name": row["left_name"], "path": row["left_path"],
                     "image_count": row["left_image_count"], "size_bytes": row["left_size_bytes"],
                     "modified_at": row["left_modified_at"]}
        right_file = {"id": row["right_id"], "name": row["right_name"], "path": row["right_path"],
                      "image_count": row["right_image_count"], "size_bytes": row["right_size_bytes"],
                      "modified_at": row["right_modified_at"]}
        newer_file, older_file = sorted(
            (left_file, right_file), key=lambda file: (file["modified_at"], file["id"]), reverse=True
        )
        matches.append({
            "score": row["score"],
            "median_distance": row["median_distance"],
            "left": newer_file,
            "right": older_file,
            "verdict": verdict,
        })
    matches.sort(key=lambda item: (-item["left"]["modified_at"], -item["score"], item["left"]["name"]))
    if not stale:
        errors = [
            {"id": row["comic_id"], "name": next(c["name"] for c in comics if c["id"] == row["comic_id"]), "error": row["error"]}
            for row in fingerprint_rows if row["error"]
        ]
    return {"checked": len(samples), "recalculated": len(stale), "matches": matches[:200], "errors": errors}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    port = 8765
    uvicorn.run(app, host="127.0.0.1", port=port)
