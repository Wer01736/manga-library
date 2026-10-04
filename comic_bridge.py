from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, unquote, urlparse, urlsplit, urlunparse
from urllib.request import Request, urlopen

from PIL import Image


HOST = "127.0.0.1"
PORT = 8766
ORIGIN = "http://127.0.0.1:8765"
BRIDGE_VERSION = "1.3.3"
MIN_NHENTAI_EXTENSION_VERSION = (0, 6, 7)
APP_DIR = Path(
    os.environ.get(
        "COMIC_WEB_ROOT",
        Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent,
    )
)
DATA_DIR = Path(os.environ.get("COMIC_WEB_DATA", APP_DIR / "data"))
LIBRARY_DB = DATA_DIR / "library.db"
MANIFEST_FILE = DATA_DIR / "18comic_manifests.json"
JOBS_FILE = DATA_DIR / "web_download_jobs_18comic.json"
SOURCES_FILE = DATA_DIR / "web_download_sources.json"
CORE_JOBS_FILE = DATA_DIR / "web_download_jobs.json"
LOCK = threading.RLock()
# The packaged core marks unseen files as missing during each full-root scan.
# Allow three downloads; finalize_worker still serializes library scans.
DOWNLOAD_SLOTS = threading.Semaphore(3)
MAX_TRANSIENT_RETRIES = 3
REPAIR_QUEUE_FAILURE_THRESHOLD = 3
COMPLETED_JOB_RETENTION_SECONDS = 3600
FINALIZE_EVENT = threading.Event()
PENDING_FINALIZE = {}
EXTENSION_DIAGNOSTICS = []
BROWSER_PAGE_CONDITION = threading.Condition()
BROWSER_PAGES = {}
BROWSER_ERRORS = {}
STARTUP_STATE = {
    "state": "starting",
    "message": "下載橋接正在啟動",
    "updated_at": int(time.time()),
}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024


def version_tuple(value):
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:3]]
    return tuple((parts + [0, 0, 0])[:3])


@contextmanager
def library_connection(*, read_only=False, timeout=10):
    """Commit or roll back writes and always release the SQLite file handle."""
    target = f"file:{LIBRARY_DB.as_posix()}?mode=ro" if read_only else LIBRARY_DB
    connection = sqlite3.connect(target, timeout=timeout, uri=read_only)
    try:
        yield connection
        if not read_only:
            connection.commit()
    except Exception:
        if not read_only:
            connection.rollback()
        raise
    finally:
        connection.close()


def ensure_library_extensions():
    """Create bridge-owned tables without changing the packaged core schema."""
    with library_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS download_sources (
                id INTEGER PRIMARY KEY,
                comic_id INTEGER,
                site_key TEXT NOT NULL,
                source_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                request_url TEXT NOT NULL DEFAULT '',
                archive_path TEXT NOT NULL,
                archive_name TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                verified_at INTEGER NOT NULL,
                UNIQUE(site_key, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_download_sources_comic ON download_sources(comic_id);
            CREATE INDEX IF NOT EXISTS idx_download_sources_path ON download_sources(archive_path);
            CREATE TABLE IF NOT EXISTS download_history (
                job_id TEXT PRIMARY KEY,
                site_key TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                archive_path TEXT NOT NULL DEFAULT '',
                archive_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                total_pages INTEGER NOT NULL DEFAULT 0,
                raw_bytes INTEGER NOT NULL DEFAULT 0,
                restored_bytes INTEGER NOT NULL DEFAULT 0,
                archive_bytes INTEGER NOT NULL DEFAULT 0,
                size_ratio REAL,
                created_at INTEGER NOT NULL DEFAULT 0,
                finished_at INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS archive_health (
                archive_path TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                checked_at INTEGER NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS repair_queue (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                site_key TEXT NOT NULL,
                site_label TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                source_key TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                origin TEXT NOT NULL DEFAULT 'manual'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_repair_queue_source_key
            ON repair_queue(source_key) WHERE source_key <> '';
        """)


SITE_LABELS = {"18comic": "禁漫", "nhentai": "nhentai", "wnacg": "紳士", "unknown": "未知"}


def normalized_title(value):
    """Compare the displayed work name across sites without using the source URL."""
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = re.sub(r"\.(?:zip|cbz|rar|7z)$", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", "", value).casefold()


def repair_queue_conflict(conn, title, source_key="", exclude_id=None):
    if source_key:
        row = conn.execute(
            "SELECT id,title FROM repair_queue WHERE source_key=? AND (? IS NULL OR id<>?)",
            (source_key, exclude_id, exclude_id),
        ).fetchone()
        if row:
            return "queue", row
    title_key = normalized_title(title)
    if not title_key:
        return "", None
    for row in conn.execute("SELECT id,title FROM repair_queue"):
        if exclude_id is not None and row[0] == exclude_id:
            continue
        if normalized_title(row[1]) == title_key:
            return "queue", row
    for row in conn.execute("SELECT id,name,title_guess FROM comics WHERE status='available'"):
        if title_key in {normalized_title(row[1]), normalized_title(row[2])}:
            return "library", row
    return "", None


def repair_queue_items():
    with library_connection(read_only=True) as conn:
        rows = conn.execute("SELECT id,title,site_key,site_label,source_url FROM repair_queue ORDER BY sort_order,id").fetchall()
    return [{"id": row[0], "title": row[1], "site_key": row[2], "site_label": row[3], "source_url": row[4]} for row in rows]


def add_repair_queue(payload):
    title = str(payload.get("title") or "").strip() or "未取得名稱"
    source_url = str(payload.get("source_url") or "").strip()
    if source_url and not source_url.startswith(("http://", "https://")):
        raise ValueError("網址格式不正確")
    source_key, inferred_site, _ = source_parts(source_url)
    site_key = str(payload.get("site_key") or inferred_site or "unknown").strip().lower()
    site_label = SITE_LABELS.get(site_key, str(payload.get("site_label") or "未知").strip()[:20] or "未知")
    origin = str(payload.get("origin") or "manual")[:30]
    with library_connection() as conn:
        conflict, _ = repair_queue_conflict(conn, title, source_key)
        if conflict == "queue":
            raise ValueError("這部同名漫畫已在待補清單")
        if conflict == "library":
            raise ValueError("書庫已有同名漫畫，不需要加入待補")
        order = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM repair_queue").fetchone()[0]
        cursor = conn.execute("INSERT INTO repair_queue(title,site_key,site_label,source_url,source_key,sort_order,origin) VALUES(?,?,?,?,?,?,?)", (title, site_key, site_label, source_url, source_key, order, origin))
        return {"id": cursor.lastrowid, "title": title, "site_key": site_key, "site_label": site_label, "source_url": source_url}


def remove_repair_queue(ids):
    if not isinstance(ids, list) or any(not isinstance(value, int) for value in ids):
        raise ValueError("待補紀錄 ID 格式不正確")
    with library_connection() as conn:
        conn.executemany("DELETE FROM repair_queue WHERE id=?", [(value,) for value in ids])


def update_repair_queue(item_id, payload):
    title = str(payload.get("title") or "").strip() or "未取得名稱"
    source_url = str(payload.get("source_url") or "").strip()
    if source_url and not source_url.startswith(("http://", "https://")):
        raise ValueError("網址格式不正確")
    source_key, inferred_site, _ = source_parts(source_url)
    site_key = str(payload.get("site_key") or inferred_site or "unknown").strip().lower()
    site_label = str(payload.get("site_label") or SITE_LABELS.get(site_key, "未知")).strip()[:20] or "未知"
    with library_connection() as conn:
        if not conn.execute("SELECT 1 FROM repair_queue WHERE id=?", (item_id,)).fetchone():
            raise ValueError("待補紀錄不存在")
        conflict, _ = repair_queue_conflict(conn, title, source_key, item_id)
        if conflict == "queue":
            raise ValueError("這部同名漫畫已在待補清單")
        if conflict == "library":
            raise ValueError("書庫已有同名漫畫，不需要保留在待補")
        conn.execute("UPDATE repair_queue SET title=?,site_key=?,site_label=?,source_url=?,source_key=? WHERE id=?", (title, site_key, site_label, source_url, source_key, item_id))
    return {"id": item_id, "title": title, "site_key": site_key, "site_label": site_label, "source_url": source_url}


def failed_attempt_count(job):
    _, site_key, source_id = source_parts(job.get("source_url") or job.get("request_url") or "")
    history_count = 0
    if site_key and source_id:
        with library_connection() as conn:
            history_count = conn.execute(
                "SELECT COUNT(*) FROM download_history WHERE site_key=? AND source_id=? AND status='failed'",
                (site_key, source_id),
            ).fetchone()[0]
    return max(history_count, int(job.get("retry_count") or 0) + 1)


def maybe_add_failed_job_to_repair_queue(job):
    """Add only a repeatedly failed download with no finished ZIP or same-name library work."""
    if job.get("status") != "failed":
        return {"state": "not_failed", "message": ""}
    if job.get("archive_created") and Path(job.get("archive_path") or "").is_file():
        return {"state": "indexing_failed", "message": "ZIP 已完成，只需重新加入書庫"}
    source_url = str(job.get("source_url") or job.get("request_url") or "").strip()
    title = str(job.get("title") or job.get("archive_name") or "").strip()
    if not source_url or not title:
        return {"state": "incomplete", "message": "缺少名稱或來源網址，請手動確認後加入待補"}
    attempts = failed_attempt_count(job)
    if attempts < REPAIR_QUEUE_FAILURE_THRESHOLD:
        return {"state": "retry_pending", "message": f"已失敗 {attempts} 次，達 {REPAIR_QUEUE_FAILURE_THRESHOLD} 次後自動加入待補"}
    if known_physical_source(source_url):
        return {"state": "duplicate_library", "message": "相同來源已在書庫"}
    try:
        item = add_repair_queue({
            "title": title,
            "source_url": source_url,
            "site_key": job.get("source_category") or "",
            "origin": "failed_download",
        })
        return {"state": "added", "message": "連續下載失敗，已自動加入待補漫畫", "item_id": item["id"]}
    except ValueError as error:
        message = str(error)
        state = "duplicate_library" if "書庫已有" in message else "already_queued"
        return {"state": state, "message": message}


def resolve_repair_queue_for_completed_job(job):
    source_key, _, _ = source_parts(job.get("source_url") or job.get("request_url") or "")
    title_key = normalized_title(job.get("title") or job.get("archive_name") or "")
    with library_connection() as conn:
        rows = conn.execute("SELECT id,title,source_key FROM repair_queue").fetchall()
        ids = [row[0] for row in rows if (source_key and row[2] == source_key) or
               (title_key and normalized_title(row[1]) == title_key)]
        if ids:
            conn.executemany("DELETE FROM repair_queue WHERE id=?", [(item_id,) for item_id in ids])
    return len(ids)


def source_parts(value):
    key = canonical_source_key(value)
    if not key or ":" not in key:
        return "", "", ""
    site_key, source_id = key.split(":", 1)
    return key, site_key, source_id


def known_physical_source(value):
    """Return an exact source match only when its recorded ZIP still exists."""
    key, site_key, source_id = source_parts(value)
    if not key:
        return None
    try:
        with library_connection(read_only=True) as conn:
            row = conn.execute("SELECT archive_path,archive_name FROM download_sources WHERE site_key=? AND source_id=? ORDER BY verified_at DESC LIMIT 1", (site_key, source_id)).fetchone()
        if row and Path(row[0]).is_file():
            return {"archive_path": row[0], "archive_name": row[1] or Path(row[0]).name}
    except sqlite3.Error:
        pass
    # Legacy fallback: old builds wrote source records only to JSON.
    record = SOURCES.get(key) or {}
    path = Path(record.get("archive_path") or "")
    if path.is_file():
        return {"archive_path": str(path), "archive_name": path.name}
    return None


def validate_archive(path, expected_pages=0, deep=False):
    """Return a stable archive-health payload; never writes into the archive."""
    path = str(path)
    if not os.path.isfile(path):
        return {"status": "missing", "image_count": 0, "error": "ZIP 檔案不存在"}
    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                return {"status": "damaged", "image_count": 0, "error": f"ZIP 成員損壞：{bad_member}"}
            members = [info for info in archive.infolist()
                       if not info.is_dir() and Path(info.filename).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS]
            if not members:
                return {"status": "damaged", "image_count": 0, "error": "ZIP 沒有可讀圖片"}
            if expected_pages and len(members) != int(expected_pages):
                return {"status": "damaged", "image_count": len(members),
                        "error": f"頁數不符：預期 {expected_pages}，實際 {len(members)}"}
            if deep:
                for info in members:
                    with archive.open(info) as stream:
                        image = Image.open(stream)
                        image.verify()
            return {"status": "healthy", "image_count": len(members), "error": ""}
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as error:
        return {"status": "damaged", "image_count": 0, "error": str(error)}


def record_archive_health(path, result):
    try:
        with library_connection() as conn:
            conn.execute("""
                INSERT INTO archive_health(archive_path,status,checked_at,image_count,error)
                VALUES(?,?,?,?,?)
                ON CONFLICT(archive_path) DO UPDATE SET
                    status=excluded.status, checked_at=excluded.checked_at,
                    image_count=excluded.image_count, error=excluded.error
            """, (str(path), result["status"], int(time.time()), int(result.get("image_count") or 0), result.get("error") or ""))
    except sqlite3.Error:
        pass


def archive_health_by_path():
    try:
        with library_connection(read_only=True) as conn:
            return {path: {"integrity_status": status, "integrity_error": error, "integrity_checked_at": checked_at}
                    for path, status, checked_at, error in conn.execute(
                        "SELECT archive_path,status,checked_at,error FROM archive_health")}
    except sqlite3.Error:
        return {}


def existing_missing_paths():
    """Return only missing DB records whose archive still exists on disk.

    The core scanner can overlap while several downloads finish.  Until its scanner
    is rebuilt with exclusive execution, this compatibility layer prevents a
    transient incorrect ``missing`` marker from hiding a readable archive.
    """
    try:
        with library_connection(read_only=True) as conn:
            rows = conn.execute("SELECT id, path FROM comics WHERE status='missing'").fetchall()
        return {comic_id: path for comic_id, path in rows if os.path.isfile(path)}
    except (sqlite3.Error, OSError):
        return {}


def with_status(request_path, status):
    parsed = urlsplit(request_path)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "status"]
    query.append(("status", status))
    return urlunparse(("", "", parsed.path, "", urlencode(query), ""))


def sort_library_items(items, request_path):
    query = dict(parse_qsl(urlsplit(request_path).query, keep_blank_values=True))
    sort_key = query.get("sort", "modified_at")
    descending = query.get("direction", "desc") != "asc"
    if sort_key in {"size_bytes", "modified_at"}:
        key = lambda item: (item.get(sort_key) or 0, str(item.get("name") or "").casefold())
    else:
        key = lambda item: (str(item.get(sort_key) or "").casefold(), str(item.get("name") or "").casefold())
    return sorted(items, key=key, reverse=descending)


def merged_library_response(request_path):
    """Merge normal items with real files temporarily marked missing by the core."""
    status, available = proxy_json(with_status(request_path, "available"))
    if status != 200 or not isinstance(available, dict):
        return status, available
    existing = existing_missing_paths()
    restored = []
    if existing:
        missing_status, missing = proxy_json(with_status(request_path, "missing"))
        if missing_status == 200 and isinstance(missing, dict):
            restored = [item for item in missing.get("items", []) if item.get("id") in existing]
            for item in restored:
                item["status"] = "available"
                item["error"] = ""
    health = archive_health_by_path()
    items = sort_library_items([*(available.get("items") or []), *restored], request_path)
    for item in items:
        item.update(health.get(item.get("path"), {}))
    authors = sorted({str(item.get("author") or "") for item in items if item.get("author")}, key=str.casefold)
    groups = sorted({str(item.get("group_name") or "") for item in items if item.get("group_name")}, key=str.casefold)
    return 200, {
        **available,
        "items": items,
        "authors": authors,
        "groups": groups,
        "marker_count": sum(1 for item in items if item.get("reading_marker")),
    }


def reconciled_roots_response():
    status, roots = proxy_json("/api/roots")
    if status != 200 or not isinstance(roots, list):
        return status, roots
    existing = existing_missing_paths()
    try:
        with library_connection(read_only=True) as conn:
            available = dict(conn.execute("SELECT id, path FROM comics WHERE status='available'").fetchall())
    except sqlite3.Error:
        return 200, roots
    all_paths = {**available, **existing}
    total = len(all_paths)
    result = []
    for root in roots:
        root_path = os.path.normcase(os.path.abspath(root.get("path") or ""))
        prefix = root_path.rstrip("\\/") + os.sep
        count = sum(1 for path in all_paths.values()
                    if os.path.normcase(os.path.abspath(path)) == root_path or
                    os.path.normcase(os.path.abspath(path)).startswith(prefix))
        result.append({**root, "comic_count": count, "total_count": total})
    return 200, result


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def canonical_source_key(value):
    """Use a stable source identity instead of comparing raw copied URLs."""
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host == "18comic.vip":
            match = re.search(r"/(?:album|photo)/(\d+)", parsed.path)
            return f"18comic:{match.group(1)}" if match else ""
        if host in {"wnacg.com", "wnacg.ru"}:
            match = re.search(r"aid-(\d+)", parsed.path)
            return f"wnacg:{match.group(1)}" if match else ""
        if host == "nhentai.net":
            match = re.search(r"/g/(\d+)", parsed.path)
            return f"nhentai:{match.group(1)}" if match else ""
        return f"{host}:{parsed.path.rstrip('/')}" if host and parsed.path else ""
    except Exception:
        return ""


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


MANIFESTS = {}
SOURCES = {}
JOBS = {}


def load_runtime_state():
    """Load and normalize persisted bridge state only during explicit startup."""
    global MANIFESTS, SOURCES, JOBS
    manifests = load_json(MANIFEST_FILE, {})
    sources = load_json(SOURCES_FILE, {})
    loaded_jobs = [
        job for job in load_json(JOBS_FILE, [])
        if isinstance(job, dict) and job.get("id")
    ]
    jobs = {job["id"]: job for job in loaded_jobs if job.get("status") != "completed"}
    if len(jobs) != len(loaded_jobs):
        save_json(JOBS_FILE, list(jobs.values()))

    seen_active = set()
    for job_id, job in list(jobs.items()):
        job["_cancel"] = threading.Event()
        manifest = manifests.get(str(job.get("manifest_key") or job.get("album_id") or ""), {})
        archive_ready = job.get("archive_created") and Path(job.get("archive_path", "")).is_file()
        if (
            not archive_ready
            and manifest.get("source_site") == "nhentai"
            and version_tuple(manifest.get("extension_version")) < MIN_NHENTAI_EXTENSION_VERSION
        ):
            job["status"] = "failed"
            job["finished_at"] = int(time.time())
            job["error"] = "舊版 nhentai 工作不支援瀏覽器圖片傳輸"
            job["message"] = "已停止舊版工作；請重新載入最新版擴充功能後送出"
            continue
        transient_codes = (
            "HTTP Error 429", "HTTP Error 500", "HTTP Error 502",
            "HTTP Error 503", "HTTP Error 504", "HTTP Error 522",
            "timed out", "逾時",
        )
        if job.get("status") == "failed" and any(
            code in str(job.get("error", "")) for code in transient_codes
        ):
            job["status"] = "waiting"
            job["completed"] = 0
            job["retry_count"] = int(job.get("retry_count", 0)) + 1
            job["message"] = "來源先前暫時失敗，程式啟動後自動重新下載"
        if job.get("status") in {"waiting", "downloading", "finalizing"}:
            key = (
                str(job.get("manifest_key") or job.get("album_id") or ""),
                os.path.normcase(job.get("root_path", "")),
            )
            if key in seen_active:
                jobs.pop(job_id, None)
                continue
            seen_active.add(key)
            archive_exists = Path(job.get("archive_path", "")).exists()
            if job.get("status") == "finalizing" and archive_exists:
                job["message"] = "ZIP 已完成，等待加入書庫"
            else:
                Path(str(job.get("archive_path", "")) + ".part").unlink(missing_ok=True)
                job["status"] = "waiting"
                job["completed"] = 0
                job["message"] = "程式重新啟動，等待繼續下載"

    MANIFESTS = manifests if isinstance(manifests, dict) else {}
    SOURCES = sources if isinstance(sources, dict) else {}
    JOBS = jobs


def persist_jobs():
    with LOCK:
        save_json(JOBS_FILE, [public_job(job) for job in JOBS.values()])


def remember_completed_source(job):
    key, site_key, source_id = source_parts(job.get("source_url") or job.get("request_url") or "")
    path = job.get("archive_path") or ""
    if not key or not path or not os.path.isfile(path):
        return
    record = {
        "source_url": job.get("source_url") or "",
        "archive_path": path,
        "archive_name": job.get("archive_name") or Path(path).name,
        "source_category": job.get("source_category") or "",
        "finished_at": int(job.get("finished_at") or time.time()),
    }
    with LOCK:
        SOURCES[key] = record
    try:
        with library_connection() as conn:
            row = conn.execute("SELECT id FROM comics WHERE path=?", (path,)).fetchone()
            conn.execute("""
                INSERT INTO download_sources(comic_id,site_key,source_id,canonical_url,request_url,archive_path,archive_name,created_at,verified_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(site_key,source_id) DO UPDATE SET
                    comic_id=excluded.comic_id, canonical_url=excluded.canonical_url,
                    request_url=excluded.request_url, archive_path=excluded.archive_path,
                    archive_name=excluded.archive_name, verified_at=excluded.verified_at
            """, (row[0] if row else None, site_key, source_id, job.get("source_url") or "",
                  job.get("request_url") or "", path, record["archive_name"],
                  record["finished_at"], int(time.time())))
    except sqlite3.Error as error:
        # Keep a recoverable legacy fallback only if the canonical DB write fails.
        with LOCK:
            save_json(SOURCES_FILE, SOURCES)
            EXTENSION_DIAGNOSTICS.append({
                "time": int(time.time()),
                "kind": "source_persistence_fallback",
                "error": str(error),
            })


def record_download_history(job, status=None, error=None):
    key, site_key, source_id = source_parts(job.get("source_url") or job.get("request_url") or "")
    try:
        with library_connection() as conn:
            conn.execute("""
                INSERT INTO download_history(job_id,site_key,source_id,source_url,archive_path,archive_name,status,total_pages,
                    raw_bytes,restored_bytes,archive_bytes,size_ratio,created_at,finished_at,error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status, archive_path=excluded.archive_path, archive_name=excluded.archive_name,
                    total_pages=excluded.total_pages, raw_bytes=excluded.raw_bytes,
                    restored_bytes=excluded.restored_bytes, archive_bytes=excluded.archive_bytes,
                    size_ratio=excluded.size_ratio, finished_at=excluded.finished_at, error=excluded.error
            """, (str(job.get("id") or uuid.uuid4().hex), site_key, source_id, job.get("source_url") or "",
                  job.get("archive_path") or "", job.get("archive_name") or "", status or job.get("status") or "",
                  int(job.get("total") or 0), int(job.get("raw_bytes") or 0), int(job.get("restored_bytes") or 0),
                  int(job.get("archive_bytes") or 0), job.get("size_ratio"), int(job.get("created_at") or 0),
                  int(job.get("finished_at") or time.time()), error if error is not None else job.get("error") or ""))
    except sqlite3.Error:
        pass


def expire_completed_job(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if job and job.get("status") == "completed":
            JOBS.pop(job_id, None)
            persist_jobs()


def archive_completed_core_jobs():
    """Validate and archive completed core work exactly once."""
    jobs = load_json(CORE_JOBS_FILE, [])
    if not isinstance(jobs, list):
        return
    pending = []
    changed = False
    for job in jobs:
        if not isinstance(job, dict) or job.get("status") != "completed":
            pending.append(job)
            continue
        health = validate_archive(job.get("archive_path") or "")
        record_archive_health(job.get("archive_path") or "", health)
        remember_completed_source(job)
        record_download_history(job, "completed" if health["status"] == "healthy" else "unverified_completed", health.get("error"))
        changed = True
    if changed:
        save_json(CORE_JOBS_FILE, pending)


def http_json(url, method="GET", payload=None, headers=None, timeout=60):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, method=method, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
            return response.status, json.loads(data.decode("utf-8")) if data else {}
    except HTTPError as error:
        data = error.read()
        try:
            result = json.loads(data.decode("utf-8"))
        except Exception:
            result = {"detail": data.decode("utf-8", "replace") or str(error)}
        return error.code, result


def proxy_json(path, method="GET", payload=None, timeout=60):
    return http_json(ORIGIN + path, method, payload, timeout=timeout)


def album_id(value):
    try:
        parsed = urlparse(value)
        if parsed.hostname not in {"18comic.vip", "www.18comic.vip"}:
            return None
        match = re.search(r"/(?:album|photo)/(\d+)", parsed.path)
        return match.group(1) if match else None
    except Exception:
        return None


def source_manifest_ref(value):
    """Return (site, source id, manifest key) for browser-resolved sources."""
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host == "18comic.vip":
            match = re.search(r"/(?:album|photo)/(\d+)", parsed.path)
            return ("18comic", match.group(1), match.group(1)) if match else None
        if host == "nhentai.net":
            match = re.search(r"/g/(\d+)", parsed.path)
            return ("nhentai", match.group(1), f"nhentai:{match.group(1)}") if match else None
    except Exception:
        pass
    return None


def manifest_identity(payload, default_site="18comic"):
    site = str(payload.get("source_site") or default_site).strip().lower()
    source_id = str(payload.get("source_id") or payload.get("album_id") or "").strip()
    if site not in {"18comic", "nhentai"} or not source_id.isdigit():
        raise ValueError("漫畫來源或作品編號不正確")
    key = source_id if site == "18comic" else f"nhentai:{source_id}"
    return site, source_id, key


def clean_filename(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "未命名漫畫")
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if value.upper() in reserved:
        value = "_" + value
    return value[:180] or "未命名漫畫"


def version_tuple(value):
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:3]]
    return tuple((parts + [0, 0, 0])[:3])


def preview_for(manifest):
    site = manifest.get("source_site") or "18comic"
    source_id = str(manifest.get("source_id") or manifest.get("album_id") or "")
    title = manifest.get("title") or f"{site} {source_id}"
    return {
        "url": manifest.get("gallery_url") or manifest.get("album_url") or manifest.get("reader_url") or manifest.get("photo_url"),
        "title": title,
        "page_count": len(manifest.get("images") or []),
        "archive_name": clean_filename(title) + ".zip",
        "source_category": site,
        "source_tags": list(dict.fromkeys(manifest.get("tags") or [])),
        "author": manifest.get("author") or "",
        "cover_url": manifest.get("cover_url") or "",
        "description": manifest.get("description") or "",
        "album_id": source_id,
        "source_id": source_id,
        "source_site": site,
        "language": manifest.get("language") or "unknown",
        "extension_version": manifest.get("extension_version") or "",
    }


def store_source_manifest(payload, default_site="18comic"):
    site, source_id, manifest_key = manifest_identity(payload, default_site)
    images = []
    for entry in payload.get("images") or []:
        raw_candidates = entry if isinstance(entry, list) else [entry]
        candidates = list(dict.fromkeys(
            str(url).strip() for url in raw_candidates if isinstance(url, str) and str(url).strip()
        ))
        if not candidates or any(not url.startswith("https://") for url in candidates):
            raise ValueError("漫畫頁資料不完整，請重新載入來源頁")
        if site == "nhentai":
            for image_url in candidates:
                parsed = urlparse(image_url)
                image_host = (parsed.hostname or "").lower()
                if not (image_host == "nhentai.net" or image_host.endswith(".nhentai.net")) or "/galleries/" not in parsed.path:
                    raise ValueError("nhentai 圖片網址格式不正確")
        images.append(candidates[0] if len(candidates) == 1 else candidates)
    if not images:
        raise ValueError("漫畫頁資料不完整，請重新載入來源頁")
    declared_pages = int(payload.get("page_count") or 0)
    if declared_pages and declared_pages != len(images):
        raise ValueError(f"頁數不符：目錄顯示 {declared_pages}，實際取得 {len(images)}")
    normalized = {
        **payload,
        "source_site": site,
        "source_id": source_id,
        "album_id": source_id,
        "images": images,
        "tags": list(dict.fromkeys(str(tag).strip() for tag in payload.get("tags", []) if str(tag).strip()))[:50],
        "languages": list(dict.fromkeys(str(language).strip().lower() for language in payload.get("languages", []) if str(language).strip()))[:10],
        "language": str(payload.get("language") or "unknown").strip().lower(),
    }
    with LOCK:
        MANIFESTS[manifest_key] = normalized
        save_json(MANIFEST_FILE, MANIFESTS)
    return normalized


def valid_roots():
    status, result = proxy_json("/api/roots")
    roots = {os.path.normcase(os.path.abspath(item["path"])) for item in result} if status == 200 else set()
    test_root = os.environ.get("COMIC_BRIDGE_TEST_ROOT")
    if test_root:
        roots.add(os.path.normcase(os.path.abspath(test_root)))
    return roots


def ensure_free_space(root_path: str):
    """Reject a new bridge download before it can consume the last free disk space."""
    free_bytes = shutil.disk_usage(root_path).free
    if free_bytes < MIN_FREE_BYTES:
        free_gib = free_bytes / (1024 ** 3)
        reserve_gib = MIN_FREE_BYTES / (1024 ** 3)
        raise ValueError(f"磁碟剩餘空間不足：目前 {free_gib:.1f} GiB，下載前至少需保留 {reserve_gib:.0f} GiB")


def maintenance_status():
    result = {"history": 0, "sources": 0, "health": {}}
    try:
        with library_connection(read_only=True) as conn:
            result["history"] = conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
            result["sources"] = conn.execute("SELECT COUNT(*) FROM download_sources").fetchone()[0]
            result["health"] = dict(conn.execute("SELECT status, COUNT(*) FROM archive_health GROUP BY status"))
    except sqlite3.Error as error:
        result["error"] = str(error)
    return result


def validate_root(root):
    absolute = os.path.abspath(root)
    if os.path.normcase(absolute) not in valid_roots():
        raise ValueError("下載位置不是目前書庫的掃描資料夾")
    Path(absolute).mkdir(parents=True, exist_ok=True)
    return absolute


def fetch_image(url, referer, cancel_event, source_site="18comic"):
    last_error = None
    parsed = urlparse(url)
    hosts = [parsed.netloc]
    if source_site == "18comic":
        hosts = list(dict.fromkeys([*hosts, "cdn-msp2.18comic.vip", "cdn-msp.18comic.vip", "cdn-msp3.18comic.vip"]))
    for attempt in range(8):
        if cancel_event.is_set():
            raise InterruptedError("下載已取消")
        for host in hosts:
            candidate = urlunparse(parsed._replace(netloc=host))
            try:
                request = Request(candidate, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
                    "Referer": referer,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                })
                with urlopen(request, timeout=45) as response:
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if source_site == "nhentai" and content_type and not content_type.startswith("image/"):
                        raise RuntimeError(f"圖片網址回傳非圖片內容：{content_type}")
                    data = response.read()
                    if not data:
                        raise RuntimeError("圖片網址回傳空白內容")
                    return data
            except HTTPError as error:
                last_error = error
                if source_site == "nhentai" and error.code in {400, 401, 403, 404}:
                    raise RuntimeError(f"HTTP {error.code}：{candidate}") from error
            except Exception as error:
                last_error = error
        if attempt < 7:
            time.sleep(min(2 * (attempt + 1), 10))
    raise RuntimeError(f"圖片下載失敗：{last_error}")


def fetch_image_entry(entry, referer, cancel_event, source_site="18comic"):
    candidates = entry if isinstance(entry, list) else [entry]
    errors = []
    for candidate in candidates:
        try:
            return fetch_image(candidate, referer, cancel_event, source_site), candidate
        except InterruptedError:
            raise
        except Exception as error:
            errors.append(str(error))
    raise RuntimeError("所有候選網址皆失敗：" + "；".join(errors))


def wait_for_browser_page(job_id, index, cancel_event, timeout=90):
    deadline = time.monotonic() + timeout
    with BROWSER_PAGE_CONDITION:
        while True:
            if cancel_event.is_set():
                raise InterruptedError("下載已取消")
            browser_error = BROWSER_ERRORS.pop(job_id, "")
            if browser_error:
                raise RuntimeError(browser_error)
            page = BROWSER_PAGES.get(job_id, {}).pop(index, None)
            if page:
                return page
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"瀏覽器未在 {timeout} 秒內傳送第 {index} 頁")
            BROWSER_PAGE_CONDITION.wait(min(remaining, 1))


def slice_count(scramble_id, aid, filename):
    scramble_id = int(scramble_id or 220980)
    aid = int(aid)
    if aid < scramble_id:
        return 0
    digest = hashlib.md5(f"{aid}{filename}".encode()).hexdigest()
    code = ord(digest[-1])
    if aid < 268850:
        return 10
    if aid < 421926:
        code %= 10
    else:
        code %= 8
    return (code + 1) * 2


def restore_image(raw, aid, source_url, scramble_id=220980):
    source = Image.open(io.BytesIO(raw)).convert("RGB")
    filename = Path(unquote(urlparse(source_url).path)).stem
    count = slice_count(scramble_id, aid, filename)
    width, height = source.size
    remainder = height % count
    target = Image.new("RGB", source.size)
    for index in range(count):
        strip_height = height // count
        destination_y = strip_height * index
        source_y = height - strip_height * (index + 1) - remainder
        if index == 0:
            strip_height += remainder
        else:
            destination_y += remainder
        target.paste(source.crop((0, source_y, width, source_y + strip_height)), (0, destination_y))
    output = io.BytesIO()
    target.save(output, "PNG", optimize=True)
    return output.getvalue()


def update_job(job_id, **changes):
    with LOCK:
        JOBS[job_id].update(changes)
        persist_jobs()


def apply_metadata(job, manifest, library):
    if os.environ.get("COMIC_BRIDGE_SKIP_SCAN") == "1":
        return True
    target = os.path.normcase(os.path.abspath(job["archive_path"]))
    comic = next((item for item in library.get("items", []) if os.path.normcase(os.path.abspath(item.get("path", ""))) == target), None)
    if not comic:
        return False
    comic_id = comic["id"]
    metadata_status, _ = proxy_json(f"/api/comics/{comic_id}/metadata", "PATCH", {"author": manifest.get("author") or "", "group_name": ""})
    tags_status, _ = proxy_json(f"/api/comics/{comic_id}/tags", "PUT", {"tags": job.get("selected_tags") or manifest.get("tags") or []})
    return metadata_status == 200 and tags_status == 200


def queue_finalize(job_id, manifest):
    with LOCK:
        PENDING_FINALIZE[job_id] = manifest
        FINALIZE_EVENT.set()


def index_completed_archive(job):
    archive_path = Path(job["archive_path"])
    stat = archive_path.stat()
    with zipfile.ZipFile(archive_path) as archive:
        images = [
            (info.filename, info.file_size)
            for info in archive.infolist()
            if not info.is_dir() and Path(info.filename).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ]
    expected = int(job.get("total") or 0)
    if not images or (expected and len(images) != expected):
        raise RuntimeError(f"ZIP 頁數不符：預期 {expected}，實際 {len(images)}")
    now = int(time.time())
    author = str(job.get("author") or "")
    with library_connection(timeout=60) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""
            INSERT INTO comics(path,name,extension,size_bytes,modified_at,image_count,cover_name,author,group_name,
                title_guess,volume_guess,sha256,status,last_seen,error,author_source)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                name=excluded.name, extension=excluded.extension, size_bytes=excluded.size_bytes,
                modified_at=excluded.modified_at, image_count=excluded.image_count,
                cover_name=excluded.cover_name, author=excluded.author, group_name='',
                title_guess=excluded.title_guess, status='available', last_seen=excluded.last_seen,
                error='', author_source=excluded.author_source
        """, (
            str(archive_path), archive_path.name, archive_path.suffix.lower(), stat.st_size, stat.st_mtime,
            len(images), images[0][0], author, "", str(job.get("title") or archive_path.stem), "", None,
            "available", now, "", "manual" if author else "",
        ))
        comic_id = conn.execute("SELECT id FROM comics WHERE path=?", (str(archive_path),)).fetchone()[0]
        conn.execute("DELETE FROM archive_images WHERE comic_id=?", (comic_id,))
        conn.executemany(
            "INSERT INTO archive_images(comic_id,position,name,size_bytes) VALUES(?,?,?,?)",
            [(comic_id, index, name, size) for index, (name, size) in enumerate(images)],
        )
        for tag in job.get("selected_tags") or []:
            tag = str(tag).strip()
            if not tag:
                continue
            conn.execute("INSERT OR IGNORE INTO tags(name,created_at) VALUES(?,?)", (tag, now))
            tag_row = conn.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (tag,)).fetchone()
            if tag_row:
                conn.execute("INSERT OR IGNORE INTO comic_tags(comic_id,tag_id) VALUES(?,?)", (comic_id, tag_row[0]))
    return comic_id


def finalize_worker():
    while True:
        FINALIZE_EVENT.wait()
        time.sleep(2)
        with LOCK:
            pending = dict(PENDING_FINALIZE)
            PENDING_FINALIZE.clear()
            FINALIZE_EVENT.clear()
            finished = [(job_id, JOBS[job_id], manifest) for job_id, manifest in pending.items() if job_id in JOBS]
            persist_jobs()
        for job_id, job, manifest in finished:
            try:
                health = validate_archive(job["archive_path"], job.get("total") or 0, deep=True)
                record_archive_health(job["archive_path"], health)
                if health["status"] != "healthy":
                    raise RuntimeError(f"ZIP 驗證失敗：{health['error']}")
                index_completed_archive(job)
                update_job(job_id, status="completed", finished_at=int(time.time()),
                           message=f"下載完成並已加入書庫：{Path(job['archive_path']).name}")
                remember_completed_source(job)
                resolve_repair_queue_for_completed_job(JOBS[job_id])
                record_download_history(JOBS[job_id], "completed")
                timer = threading.Timer(COMPLETED_JOB_RETENTION_SECONDS, expire_completed_job, args=(job_id,))
                timer.daemon = True
                timer.start()
            except Exception as error:
                update_job(job_id, status="failed", finished_at=int(time.time()), archive_created=True,
                           error=str(error), message=f"ZIP 已建立，但加入書庫失敗：{error}")
                record_download_history(JOBS[job_id], "failed", str(error))


def run_job(job_id):
    job = JOBS[job_id]
    manifest_key = str(job.get("manifest_key") or job["album_id"])
    manifest = MANIFESTS[manifest_key]
    source_site = manifest.get("source_site") or "18comic"
    cancel_event = job["_cancel"]
    part_path = Path(job["archive_path"] + ".part")
    final_path = Path(job["archive_path"])
    raw_bytes = 0
    restored_bytes = 0
    try:
        update_job(job_id, status="downloading", error="", message="正在下載並封裝高畫質圖片")
        with zipfile.ZipFile(part_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, image_entry in enumerate(manifest["images"], 1):
                referer = manifest.get("reader_url") or manifest.get("photo_url") or manifest.get("gallery_url") or manifest.get("album_url")
                update_job(job_id, message=f"正在下載第 {index} / {len(manifest['images'])} 頁")
                try:
                    if job.get("delivery_mode") == "browser":
                        raw, image_url = wait_for_browser_page(job_id, index, cancel_event)
                    else:
                        raw, image_url = fetch_image_entry(image_entry, referer, cancel_event, source_site)
                except Exception as error:
                    raise RuntimeError(f"第 {index} / {len(manifest['images'])} 頁下載失敗：{error}") from error
                count = (slice_count(manifest.get("scramble_id", 220980), manifest["album_id"], Path(unquote(urlparse(image_url).path)).stem)
                         if source_site == "18comic" else 0)
                if count == 0:
                    restored = raw
                    suffix = Path(unquote(urlparse(image_url).path)).suffix.lower()
                    suffix = suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"
                else:
                    restored = restore_image(raw, manifest["album_id"], image_url, manifest.get("scramble_id", 220980))
                    suffix = ".png"
                raw_bytes += len(raw)
                restored_bytes += len(restored)
                archive.writestr(f"{index:03d}{suffix}", restored)
                update_job(job_id, completed=index, raw_bytes=raw_bytes, restored_bytes=restored_bytes,
                           message=f"正在下載、還原並封裝第 {index} / {len(manifest['images'])} 頁")
        if cancel_event.is_set():
            raise InterruptedError("下載已取消")
        update_job(job_id, status="finalizing", message="正在加入漫畫書庫並寫入作者、標籤")
        if final_path.exists():
            raise FileExistsError(f"下載完成前發現同名檔案，為避免覆蓋已停止：{final_path}")
        os.replace(part_path, final_path)
        archive_bytes = final_path.stat().st_size
        update_job(job_id, raw_bytes=raw_bytes, restored_bytes=restored_bytes, archive_created=True,
                   archive_bytes=archive_bytes,
                   size_ratio=round(archive_bytes / raw_bytes, 3) if raw_bytes else 0)
        update_job(job_id, status="finalizing", message="ZIP 已完成，等待加入書庫")
        queue_finalize(job_id, manifest)
    except InterruptedError as error:
        part_path.unlink(missing_ok=True)
        update_job(job_id, status="cancelled", finished_at=int(time.time()), message=str(error))
        record_download_history(JOBS[job_id], "cancelled", str(error))
    except Exception as error:
        part_path.unlink(missing_ok=True)
        transient = job.get("delivery_mode") != "browser" and any(
            code in str(error) for code in ("HTTP Error 429", "HTTP Error 500", "HTTP Error 502", "HTTP Error 503", "HTTP Error 504", "HTTP Error 522", "timed out", "逾時")
        )
        retry_count = int(job.get("retry_count", 0)) + 1
        if transient and retry_count <= MAX_TRANSIENT_RETRIES:
            delay_seconds = min(60, 15 * (2 ** (retry_count - 1)))
            update_job(job_id, status="waiting", completed=0, retry_count=retry_count, error=str(error),
                       message=f"來源暫時失敗，{delay_seconds} 秒後自動重新下載（第 {retry_count} / {MAX_TRANSIENT_RETRIES} 次）：{error}")
            timer = threading.Timer(delay_seconds, run_job_in_slot, args=(job_id,))
            timer.daemon = True
            timer.start()
        else:
            suffix = "（已達自動重試上限）" if transient else ""
            update_job(job_id, status="failed", finished_at=int(time.time()), error=str(error), message=f"下載失敗{suffix}：{error}")
            record_download_history(JOBS[job_id], "failed", str(error))
            repair = maybe_add_failed_job_to_repair_queue(JOBS[job_id])
            update_job(job_id, repair_queue_state=repair["state"], repair_queue_message=repair["message"])


def public_job(job):
    return {key: value for key, value in job.items() if not key.startswith("_")}


def run_job_in_slot(job_id):
    with DOWNLOAD_SLOTS:
        run_job(job_id)


def start_job(payload, retry_count=0, allow_replace=False, replacing_job_id=None):
    ref = source_manifest_ref(payload.get("url", ""))
    if not ref or ref[2] not in MANIFESTS:
        raise ValueError("尚未從瀏覽器讀取這部漫畫，請重新貼上網址再試")
    source_site, aid, manifest_key = ref
    manifest = MANIFESTS[manifest_key]
    if source_site == "nhentai" and version_tuple(manifest.get("extension_version")) < MIN_NHENTAI_EXTENSION_VERSION:
        raise ValueError("這份 nhentai 圖片清單來自舊版擴充功能；請重新載入最新版後，從來源作品頁重新送出")
    root = validate_root(payload.get("root_path", ""))
    ensure_free_space(root)
    data = preview_for(manifest)
    archive_path = Path(root) / data["archive_name"]
    with LOCK:
        duplicate = next((job for job in JOBS.values()
                          if job.get("id") != replacing_job_id
                          and job.get("manifest_key", str(job.get("album_id") or "")) == manifest_key
                          and os.path.normcase(job.get("root_path", "")) == os.path.normcase(root)
                          and job.get("status") in {"waiting", "downloading", "finalizing"}), None)
    if duplicate:
        raise ValueError("這部漫畫已在下載佇列中")
    part_path = Path(str(archive_path) + ".part")
    if not allow_replace and archive_path.exists():
        raise ValueError(f"同名壓縮檔已存在：{archive_path}")
    if part_path.exists():
        part_path.unlink()
    job_id = ("18-" if source_site == "18comic" else "nh-") + uuid.uuid4().hex
    language = str(manifest.get("language") or "").strip().lower()
    language_tag = f"language:{language}" if language and language != "unknown" else ""
    source_tag = "來源：禁漫" if source_site == "18comic" else "來源：nhentai"
    selected_tags = list(dict.fromkeys([
        *(payload.get("tags") or manifest.get("tags") or []),
        *([language_tag] if language_tag else []),
        source_tag,
    ]))
    job = {
        "id": job_id,
        "status": "waiting",
        "title": data["title"],
        "total": data["page_count"],
        "completed": 0,
        "message": "等待開始",
        "error": "",
        "archive_path": str(archive_path),
        "archive_name": archive_path.name,
        "source_url": payload["url"],
        "request_url": manifest.get("reader_url") or manifest.get("photo_url"),
        "root_path": root,
        "source_category": source_site,
        "source_tags": manifest.get("tags") or [],
        "selected_tags": selected_tags,
        "author": manifest.get("author") or "",
        "album_id": aid,
        "manifest_key": manifest_key,
        "extension_version": manifest.get("extension_version") or "",
        "delivery_mode": "browser" if source_site == "nhentai" and payload.get("delivery_mode") == "browser" else "server",
        "created_at": int(time.time()),
        "finished_at": 0,
        "retry_count": retry_count,
        "archive_created": False,
        "_cancel": threading.Event(),
    }
    with LOCK:
        JOBS[job_id] = job
        persist_jobs()
    threading.Thread(target=run_job_in_slot, args=(job_id,), daemon=True).start()
    return public_job(job)


def retry_indexing_job(job_id):
    with LOCK:
        job = JOBS[job_id]
        if not job.get("archive_created") or not Path(job.get("archive_path", "")).is_file():
            raise ValueError("這筆工作沒有可重新加入書庫的 ZIP")
        manifest = MANIFESTS.get(str(job.get("manifest_key") or job.get("album_id")))
        if not manifest:
            raise ValueError("缺少這部漫畫的來源資料，無法重新加入書庫")
        job.update(status="finalizing", error="", finished_at=0,
                   message="ZIP 已存在，正在重新加入書庫")
        persist_jobs()
    queue_finalize(job_id, manifest)
    return public_job(job)


class Handler(BaseHTTPRequestHandler):
    server_version = f"ComicBridge/{BRIDGE_VERSION}"

    def log_message(self, fmt, *args):
        if os.environ.get("COMIC_WEB_ACCESS_LOG") == "1":
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin == ORIGIN or origin.startswith(("edge-extension://", "chrome-extension://")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/repair-queue":
            items = repair_queue_items()
            return self.send_json(200, {"items": items, "total": len(items)})
        if self.path == "/api/18comic/health":
            with LOCK:
                startup = dict(STARTUP_STATE)
            return self.send_json(200, {
                "ok": True,
                "version": BRIDGE_VERSION,
                "runtime_root": str(APP_DIR.resolve()),
                "manifests": len(MANIFESTS),
                "startup": startup,
            })
        if self.path == "/api/maintenance/status":
            return self.send_json(200, maintenance_status())
        if self.path == "/api/18comic/diagnostics":
            with LOCK:
                return self.send_json(200, list(EXTENSION_DIAGNOSTICS))
        if urlsplit(self.path).path == "/api/comics":
            status, result = merged_library_response(self.path)
            return self.send_json(status, result)
        if urlsplit(self.path).path == "/api/roots":
            status, result = reconciled_roots_response()
            return self.send_json(status, result)
        if re.fullmatch(r"/api/comics/\d+", urlsplit(self.path).path):
            status, result = proxy_json(self.path)
            if status == 200 and isinstance(result, dict) and result.get("status") == "missing" and os.path.isfile(result.get("path", "")):
                result = {**result, "status": "available", "error": ""}
            return self.send_json(status, result)
        if self.path == "/api/web-download":
            status, original = proxy_json(self.path)
            original = original if status == 200 and isinstance(original, list) else []
            for job in original:
                if job.get("status") == "completed":
                    remember_completed_source(job)
                    resolve_repair_queue_for_completed_job(job)
                elif job.get("status") == "failed":
                    record_download_history(job, "failed", job.get("error") or "")
                    repair = maybe_add_failed_job_to_repair_queue(job)
                    job["repair_queue_state"] = repair["state"]
                    job["repair_queue_message"] = repair["message"]
            return self.send_json(200, original + [public_job(job) for job in JOBS.values()])
        match = re.fullmatch(r"/api/web-download/([^/]+)", self.path)
        if match and match.group(1) in JOBS:
            return self.send_json(200, public_job(JOBS[match.group(1)]))
        return self.proxy()

    def do_POST(self):
        try:
            request_path = urlsplit(self.path)
            page_match = re.fullmatch(r"/api/web-download/([^/]+)/browser-page", request_path.path)
            if page_match:
                job_id = page_match.group(1)
                job = JOBS.get(job_id)
                if not job or job.get("delivery_mode") != "browser":
                    return self.send_json(404, {"detail": "找不到等待瀏覽器傳送的下載工作"})
                query = dict(parse_qsl(request_path.query))
                index = int(query.get("index") or 0)
                image_url = query.get("url") or ""
                manifest = MANIFESTS.get(str(job.get("manifest_key") or ""), {})
                if index < 1 or index > len(manifest.get("images") or []):
                    return self.send_json(400, {"detail": "圖片頁碼不正確"})
                entry = manifest["images"][index - 1]
                allowed = entry if isinstance(entry, list) else [entry]
                if image_url not in allowed:
                    return self.send_json(400, {"detail": "圖片網址不在已驗證的候選清單"})
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 100 * 1024 * 1024:
                    return self.send_json(400, {"detail": "圖片資料大小不正確"})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.send_json(400, {"detail": "圖片資料傳送不完整"})
                with BROWSER_PAGE_CONDITION:
                    BROWSER_PAGES.setdefault(job_id, {})[index] = (raw, image_url)
                    BROWSER_PAGE_CONDITION.notify_all()
                return self.send_json(200, {"ok": True, "index": index, "bytes": len(raw)})
            payload = self.payload()
            if self.path == "/api/repair-queue":
                return self.send_json(201, add_repair_queue(payload))
            match = re.fullmatch(r"/api/repair-queue/(\d+)", self.path)
            if match:
                return self.send_json(200, update_repair_queue(int(match.group(1)), payload))
            if self.path == "/api/18comic/diagnostic":
                event = {
                    "time": int(time.time()),
                    "album_id": str(payload.get("album_id") or "")[:20],
                    "stage": str(payload.get("stage") or "")[:80],
                    "detail": str(payload.get("detail") or "")[:300],
                }
                with LOCK:
                    EXTENSION_DIAGNOSTICS.append(event)
                    del EXTENSION_DIAGNOSTICS[:-100]
                return self.send_json(200, {"ok": True})
            browser_error_match = re.fullmatch(r"/api/web-download/([^/]+)/browser-error", self.path)
            if browser_error_match and browser_error_match.group(1) in JOBS:
                job_id = browser_error_match.group(1)
                with BROWSER_PAGE_CONDITION:
                    BROWSER_ERRORS[job_id] = str(payload.get("error") or "瀏覽器無法取得圖片")[:500]
                    BROWSER_PAGE_CONDITION.notify_all()
                return self.send_json(200, {"ok": True})
            if self.path in {"/api/18comic/manifest", "/api/source/manifest"}:
                manifest = store_source_manifest(payload, "18comic" if self.path == "/api/18comic/manifest" else "")
                return self.send_json(200, {"ok": True, **preview_for(manifest)})
            if self.path == "/api/web-download/preview":
                ref = source_manifest_ref(payload.get("url", ""))
                if ref:
                    if ref[2] not in MANIFESTS:
                        return self.send_json(409, {"detail": f"正在透過瀏覽器讀取 {ref[0]} 漫畫資料，請稍後再試"})
                    return self.send_json(200, preview_for(MANIFESTS[ref[2]]))
                return self.proxy_with_payload(payload)
            if self.path == "/api/web-download/batch-preview":
                return self.batch_preview(payload)
            if self.path == "/api/web-download/start" and source_manifest_ref(payload.get("url", "")):
                known = known_physical_source(payload.get("url", ""))
                if known and not payload.get("replace_existing"):
                    return self.send_json(409, {"detail": f"相同來源已下載：{known['archive_name']}"})
                return self.send_json(200, start_job(payload, allow_replace=bool(payload.get("replace_existing"))))
            if self.path == "/api/web-download/start":
                known = known_physical_source(payload.get("url", ""))
                if known:
                    return self.send_json(409, {"detail": f"相同來源已下載：{known['archive_name']}"})
                tags = list(dict.fromkeys([*(payload.get("tags") or []), "來源：紳士"]))
                return self.proxy_with_payload({**payload, "tags": tags})
            if self.path == "/api/web-download/retry-failed":
                status, originals = proxy_json(self.path, "POST", payload)
                retried = originals if status == 200 and isinstance(originals, list) else []
                for old in list(JOBS.values()):
                    if old["status"] in {"failed", "cancelled"}:
                        if old.get("source_category") == "nhentai" and not old.get("archive_created"):
                            continue
                        try:
                            new_job = retry_indexing_job(old["id"]) if old.get("archive_created") else start_job(
                                {"url": old["source_url"], "root_path": old["root_path"], "tags": old["selected_tags"]},
                                old.get("retry_count", 0) + 1, True, old["id"])
                            if not old.get("archive_created"):
                                with LOCK:
                                    JOBS.pop(old["id"], None)
                                    persist_jobs()
                            retried.append(new_job)
                        except ValueError:
                            pass
                return self.send_json(200, retried)
            match = re.fullmatch(r"/api/web-download/([^/]+)/(cancel|retry)", self.path)
            if match and match.group(1) in JOBS:
                job = JOBS[match.group(1)]
                if match.group(2) == "cancel":
                    job["_cancel"].set()
                    return self.send_json(200, public_job(job))
                if job.get("archive_created"):
                    return self.send_json(200, retry_indexing_job(job["id"]))
                if job.get("source_category") == "nhentai":
                    raise ValueError("nhentai 圖片必須由 Edge 擴充功能重新傳送；請回來源作品頁啟動，不可由後端直接重試")
                new_job = start_job({"url": job["source_url"], "root_path": job["root_path"], "tags": job["selected_tags"]}, job.get("retry_count", 0) + 1, True, job["id"])
                with LOCK:
                    JOBS.pop(job["id"], None)
                    persist_jobs()
                return self.send_json(200, new_job)
            return self.proxy_with_payload(payload)
        except ValueError as error:
            return self.send_json(400, {"detail": str(error)})
        except Exception as error:
            return self.send_json(500, {"detail": f"漫畫下載橋接錯誤：{error}"})

    def do_PATCH(self):
        try:
            return self.proxy_with_payload(self.payload())
        except Exception as error:
            return self.send_json(500, {"detail": f"漫畫下載橋接錯誤：{error}"})

    def do_PUT(self):
        try:
            return self.proxy_with_payload(self.payload())
        except Exception as error:
            return self.send_json(500, {"detail": f"漫畫下載橋接錯誤：{error}"})

    def batch_preview(self, payload):
        urls = payload.get("urls") or []
        root = payload.get("root_path") or ""
        # Keep one list per input URL because a WNACG directory can expand
        # into any number of chapter archives.
        item_groups = [[] for _ in urls]
        other_indexes = []
        other_urls = []
        for index, url in enumerate(urls):
            known = known_physical_source(url)
            if known:
                item_groups[index] = [{"url": url, "title": known["archive_name"], "archive_name": known["archive_name"], "source_url": url, "status": "paused", "reason": "相同來源 ZIP 已存在"}]
                continue
            ref = source_manifest_ref(url)
            if not ref:
                other_indexes.append(index)
                other_urls.append(url)
                continue
            manifest_key = ref[2]
            if manifest_key not in MANIFESTS:
                item_groups[index] = [{"url": url, "title": "", "status": "error", "reason": "瀏覽器尚未讀取漫畫資料，請重新檢查"}]
                continue
            preview = preview_for(MANIFESTS[manifest_key])
            duplicate = Path(root, preview["archive_name"]).exists()
            item_groups[index] = [{**preview, "status": "paused" if duplicate else "ready", "reason": "同名壓縮檔已存在" if duplicate else ""}]
        if other_urls:
            # Proxy one source at a time so the response can safely preserve
            # expansion boundaries even when the same URL appears twice.
            for index, url in zip(other_indexes, other_urls):
                status, result = proxy_json(
                    "/api/web-download/batch-preview", "POST",
                    {"urls": [url], "root_path": root},
                )
                if status != 200:
                    return self.send_json(status, result)
                item_groups[index] = [
                    item for item in (result.get("items", []) if isinstance(result, dict) else [])
                    if isinstance(item, dict)
                ]
        items = [item for group in item_groups for item in group]
        _, original_jobs = proxy_json("/api/web-download")
        original_jobs = original_jobs if isinstance(original_jobs, list) else []
        active_by_source = {}
        for job in original_jobs:
            if job.get("status") in {"completed", "failed", "cancelled"}:
                continue
            for value in (job.get("source_url"), job.get("request_url")):
                key = canonical_source_key(value or "")
                if key:
                    active_by_source[key] = job
        _, library = proxy_json("/api/comics?status=available")
        library_items = library.get("items", []) if isinstance(library, dict) else []
        library_by_path = {os.path.normcase(os.path.abspath(comic.get("path", ""))): comic
                           for comic in library_items if comic.get("path")}
        status_labels = {"queued": "等待開始", "downloading": "下載中", "packing": "正在建立 ZIP", "finalizing": "正在加入書庫"}
        seen_sources = set()
        seen_names = set()
        for item in items:
            if item.get("status") != "ready":
                continue
            source_key = canonical_source_key(item.get("source_url") or item.get("url") or "")
            name_key = str(item.get("archive_name") or "").casefold()
            if source_key in seen_sources or (name_key and name_key in seen_names):
                item["status"] = "paused"
                item["reason"] = "本批次中有重複網址或同名作品"
                continue
            if source_key:
                seen_sources.add(source_key)
            if name_key:
                seen_names.add(name_key)
        for item in (item for item in items if item and item.get("status") == "paused"):
            source_url = item.get("source_url") or item.get("url")
            source_key = canonical_source_key(source_url or "")
            active = active_by_source.get(source_key)
            if active:
                item["duplicate_kind"] = "active_job"
                item["reason"] = f"已在下載佇列：{status_labels.get(active.get('status'), active.get('status') or '處理中')}"
                continue
            source_record = known_physical_source(source_url) if source_key else None
            if source_record:
                archive_path = os.path.abspath(source_record["archive_path"])
                comic = library_by_path.get(os.path.normcase(archive_path))
                item["duplicate_kind"] = "library" if comic else "unindexed_file"
                if comic:
                    item["comic_id"] = comic.get("id")
                item["reason"] = (f"書庫已有：{archive_path}" if comic
                                  else f"檔案存在但尚未加入書庫：{archive_path}")
                continue
            archive_name = item.get("archive_name")
            if not archive_name:
                continue
            archive_path = os.path.abspath(os.path.join(root, archive_name))
            comic = library_by_path.get(os.path.normcase(archive_path))
            if comic:
                item["duplicate_kind"] = "library"
                item["comic_id"] = comic.get("id")
                item["reason"] = f"書庫已有：{archive_path}"
            elif os.path.exists(archive_path):
                item["duplicate_kind"] = "unindexed_file"
                item["reason"] = f"檔案存在但尚未加入書庫：{archive_path}"
        return self.send_json(200, {"items": items, "ready_count": sum(item.get("status") == "ready" for item in items if item)})

    def do_DELETE(self):
        if self.path == "/api/repair-queue":
            remove_repair_queue(self.payload().get("ids", []))
            return self.send_json(200, {"ok": True})
        match = re.fullmatch(r"/api/web-download/([^/]+)", self.path)
        if match and match.group(1) in JOBS:
            job = JOBS[match.group(1)]
            if job.get("status") not in {"completed", "failed", "cancelled"}:
                return self.send_json(409, {"detail": "下載進行中，請先取消"})
            with LOCK:
                JOBS.pop(match.group(1), None)
                persist_jobs()
            return self.send_json(200, {"ok": True})
        return self.proxy()

    def proxy_with_payload(self, payload):
        status, result = proxy_json(self.path, self.command, payload)
        return self.send_json(status, result)

    def proxy(self):
        status, result = proxy_json(self.path, self.command)
        return self.send_json(status, result)


def update_startup_state(state, message, error=""):
    with LOCK:
        STARTUP_STATE.update(
            state=state,
            message=message,
            error=error,
            updated_at=int(time.time()),
        )


def wait_for_core_service(timeout=120):
    """Wait until the web library is ready before touching its persisted job list."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = proxy_json("/api/health", timeout=0.8)
            if status == 200:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    return False


def resume_persisted_jobs():
    resumable = sorted(JOBS.items(), key=lambda item: (-int(item[1].get("retry_count", 0)), int(item[1].get("created_at", 0))))
    for job_id, job in resumable:
        if job.get("status") == "finalizing" and Path(job.get("archive_path", "")).exists():
            manifest = MANIFESTS.get(str(job.get("manifest_key") or job["album_id"]))
            if manifest:
                queue_finalize(job_id, manifest)
        elif job.get("status") == "waiting":
            threading.Thread(target=run_job_in_slot, args=(job_id,), daemon=True).start()


def initialize_background_services():
    """Finish slower recovery work without delaying the bridge health endpoint."""
    threading.Thread(target=finalize_worker, daemon=True).start()
    update_startup_state("waiting_for_core", "等待書庫服務完成啟動")
    if not wait_for_core_service():
        update_startup_state(
            "degraded",
            "書庫服務尚未就緒；已保留下載紀錄，但暫不恢復背景工作",
            "等待 127.0.0.1:8765 超時",
        )
        return
    try:
        update_startup_state("maintenance", "正在背景整理已完成的下載紀錄")
        archive_completed_core_jobs()
        resume_persisted_jobs()
    except Exception as error:
        update_startup_state("degraded", "背景整理失敗；橋接仍可使用", str(error))
        with LOCK:
            EXTENSION_DIAGNOSTICS.append({
                "time": int(time.time()),
                "kind": "startup_maintenance",
                "error": str(error),
            })
        return
    update_startup_state("ready", "下載橋接已就緒")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_runtime_state()
    ensure_library_extensions()
    persist_jobs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(
        target=initialize_background_services,
        name="comic-bridge-startup",
        daemon=True,
    ).start()
    print(f"comic download bridge ready at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
