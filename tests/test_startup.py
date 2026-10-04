import os
import json
import shutil
import sqlite3
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
TEST_DATA = PROJECT_DIR / ".test_runtime" / "startup-data"
TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("COMIC_WEB_DATA", str(TEST_DATA))

import app
import comic_bridge
import main


class StartupTests(unittest.TestCase):
    def test_18comic_extension_waits_for_all_lazy_loaded_pages(self):
        manifest = json.loads((PROJECT_DIR / "edge-extension" / "manifest.json").read_text(encoding="utf-8"))
        worker = (PROJECT_DIR / "edge-extension" / "service-worker.js").read_text(encoding="utf-8")
        enhancements = (PROJECT_DIR / "static" / "enhancements.js").read_text(encoding="utf-8")

        self.assertEqual(manifest["version"], "0.7.2")
        self.assertIn('const EXTENSION_VERSION = "0.7.2"', worker)
        self.assertIn("scrape18comicReader(tab.id, albumId, album.page_count)", worker)
        self.assertIn("found >= expectedPages", worker)
        self.assertIn("window.scrollTo(0, nextTop)", worker)
        self.assertIn("url: photoUrl, active: false", worker)
        self.assertNotIn("url: photoUrl, active: true", worker)
        self.assertIn('reportDiagnostic("reader_progress"', worker)
        self.assertIn("const MIN_18COMIC_EXTENSION_VERSION = [0, 7, 2]", enhancements)
        self.assertIn("const SOURCE_RESOLVE_TIMEOUT_MS = 210000", enhancements)

    def test_18comic_reader_merges_polled_pages_in_numeric_order(self):
        worker_path = PROJECT_DIR / "edge-extension" / "service-worker.js"
        script = r"""
const fs = require('fs');
const vm = require('vm');
const worker = fs.readFileSync(process.argv[1], 'utf8');
let calls = 0;
const batches = [
  {items:[{index:1,url:'https://cdn.example/media/photos/123/1.webp'}], scramble_id:7, at_bottom:false},
  {items:[
    {index:1,url:'https://cdn.example/media/photos/123/1.webp'},
    {index:2,url:'https://cdn.example/media/photos/123/2.webp'}
  ], scramble_id:7, at_bottom:false},
  {items:[
    {index:1,url:'https://cdn.example/media/photos/123/1.webp'},
    {index:2,url:'https://cdn.example/media/photos/123/2.webp'},
    {index:3,url:'https://cdn.example/media/photos/123/3.webp'}
  ], scramble_id:7, at_bottom:true}
];
const listener = {addListener() {}};
const context = {
  chrome: {
    runtime: {onInstalled:listener, onStartup:listener, onMessage:listener},
    scripting: {async executeScript() {
      return [{result: batches[Math.min(calls++, batches.length - 1)]}];
    }}
  },
  fetch: async () => ({ok:true}),
  setTimeout: callback => { callback(); return 1; },
  clearTimeout() {},
  console,
  URL
};
vm.createContext(context);
vm.runInContext(worker + '\nglobalThis.__reader = scrape18comicReader;', context);
context.__reader(99, '123', 3).then(result => {
  console.log(JSON.stringify({calls, result}));
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""
        completed = subprocess.run(
            ["node", "-e", script, str(worker_path)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["calls"], 3)
        self.assertEqual(result["result"]["scramble_id"], 7)
        self.assertEqual(result["result"]["images"], [
            "https://cdn.example/media/photos/123/1.webp",
            "https://cdn.example/media/photos/123/2.webp",
            "https://cdn.example/media/photos/123/3.webp",
        ])

    def test_startup_removes_only_loopback_port_9_proxy(self):
        environment = {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://real-proxy.example:8080",
            "ALL_PROXY": "http://localhost:9/",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            removed = main.sanitize_blocking_proxy_environment()

            self.assertNotIn("HTTP_PROXY", os.environ)
            self.assertNotIn("ALL_PROXY", os.environ)
            self.assertEqual(os.environ["HTTPS_PROXY"], environment["HTTPS_PROXY"])
            self.assertEqual(set(removed), {"HTTP_PROXY", "ALL_PROXY"})

    def test_web_health_identifies_runtime(self):
        result = app.health()

        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["runtime_root"]), PROJECT_DIR)

    def test_bridge_binds_before_starting_slow_background_work(self):
        events = []

        class FakeServer:
            def __init__(self, address, handler):
                events.append(("bind", address))

            def serve_forever(self):
                events.append(("serve", None))

        class FakeThread:
            def __init__(self, *, target, name=None, daemon=None):
                self.target = target

            def start(self):
                events.append(("background", self.target.__name__))

        with (
            mock.patch.object(comic_bridge, "ensure_library_extensions", side_effect=lambda: events.append(("schema", None))),
            mock.patch.object(comic_bridge, "persist_jobs", side_effect=lambda: events.append(("persist", None))),
            mock.patch.object(comic_bridge, "ThreadingHTTPServer", FakeServer),
            mock.patch.object(comic_bridge.threading, "Thread", FakeThread),
        ):
            comic_bridge.main()

        labels = [item[0] for item in events]
        self.assertLess(labels.index("bind"), labels.index("background"))
        self.assertLess(labels.index("background"), labels.index("serve"))

    def test_completed_core_jobs_are_archived_once_in_background(self):
        with (
            mock.patch.object(comic_bridge.threading, "Thread") as thread,
            mock.patch.object(comic_bridge, "wait_for_core_service", return_value=True),
            mock.patch.object(comic_bridge, "archive_completed_core_jobs") as archive,
            mock.patch.object(comic_bridge, "resume_persisted_jobs") as resume,
        ):
            comic_bridge.initialize_background_services()

        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        archive.assert_called_once_with()
        resume.assert_called_once_with()
        self.assertEqual(comic_bridge.STARTUP_STATE["state"], "ready")

    def test_bridge_persisted_state_loads_only_when_requested(self):
        root = TEST_DATA / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            manifests = root / "manifests.json"
            sources = root / "sources.json"
            jobs = root / "jobs.json"
            manifests.write_text("{}", encoding="utf-8")
            sources.write_text("{}", encoding="utf-8")
            jobs.write_text(json.dumps([
                {"id": "done", "status": "completed"},
                {
                    "id": "retry", "status": "failed", "error": "HTTP Error 503",
                    "retry_count": 1, "root_path": str(root), "archive_path": str(root / "retry.zip"),
                },
            ]), encoding="utf-8")

            with (
                mock.patch.object(comic_bridge, "MANIFEST_FILE", manifests),
                mock.patch.object(comic_bridge, "SOURCES_FILE", sources),
                mock.patch.object(comic_bridge, "JOBS_FILE", jobs),
                mock.patch.object(comic_bridge, "MANIFESTS", {}),
                mock.patch.object(comic_bridge, "SOURCES", {}),
                mock.patch.object(comic_bridge, "JOBS", {}),
            ):
                comic_bridge.load_runtime_state()
                self.assertNotIn("done", comic_bridge.JOBS)
                self.assertEqual(comic_bridge.JOBS["retry"]["status"], "waiting")
                self.assertEqual(comic_bridge.JOBS["retry"]["retry_count"], 2)
        finally:
            shutil.rmtree(root)

    def test_background_failure_keeps_bridge_in_degraded_state(self):
        with (
            mock.patch.object(comic_bridge.threading, "Thread"),
            mock.patch.object(comic_bridge, "wait_for_core_service", return_value=True),
            mock.patch.object(comic_bridge, "archive_completed_core_jobs", side_effect=RuntimeError("test failure")),
        ):
            comic_bridge.initialize_background_services()

        self.assertEqual(comic_bridge.STARTUP_STATE["state"], "degraded")
        self.assertIn("test failure", comic_bridge.STARTUP_STATE["error"])

    def test_completed_source_uses_sqlite_without_rewriting_legacy_json(self):
        root = TEST_DATA / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            database = root / "library.db"
            archive = root / "comic.zip"
            legacy_sources = root / "legacy-sources.json"
            archive.write_bytes(b"test archive placeholder")
            legacy_sources.write_text('{"legacy": true}', encoding="utf-8")
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE comics (id INTEGER PRIMARY KEY, path TEXT)")
                connection.execute("INSERT INTO comics(id,path) VALUES(?,?)", (7, str(archive)))
                connection.execute("""
                    CREATE TABLE download_sources (
                        id INTEGER PRIMARY KEY, comic_id INTEGER, site_key TEXT NOT NULL,
                        source_id TEXT NOT NULL, canonical_url TEXT NOT NULL,
                        request_url TEXT NOT NULL DEFAULT '', archive_path TEXT NOT NULL,
                        archive_name TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                        verified_at INTEGER NOT NULL, UNIQUE(site_key, source_id)
                    )
                """)
                connection.commit()
            finally:
                connection.close()

            job = {
                "source_url": "https://www.wnacg.com/photos-index-aid-123.html",
                "request_url": "https://www.wnacg.com/photos-index-aid-123.html",
                "archive_path": str(archive), "archive_name": archive.name,
                "finished_at": 123456,
            }
            with (
                mock.patch.object(comic_bridge, "LIBRARY_DB", database),
                mock.patch.object(comic_bridge, "SOURCES_FILE", legacy_sources),
                mock.patch.object(comic_bridge, "SOURCES", {}),
            ):
                comic_bridge.remember_completed_source(job)
                found = comic_bridge.known_physical_source(job["source_url"])

            connection = sqlite3.connect(database)
            try:
                row = connection.execute(
                    "SELECT comic_id,site_key,source_id,archive_path FROM download_sources"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, (7, "wnacg", "123", str(archive)))
            self.assertEqual(found["archive_path"], str(archive))
            self.assertEqual(legacy_sources.read_text(encoding="utf-8"), '{"legacy": true}')
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
