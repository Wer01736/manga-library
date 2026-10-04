import io
import os
import shutil
import threading
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image


PROJECT_DIR = Path(__file__).resolve().parents[1]
TEST_WORKSPACE = PROJECT_DIR / ".test_runtime"
TEST_WORKSPACE.mkdir(exist_ok=True)
os.environ["COMIC_WEB_DATA"] = str(TEST_WORKSPACE / "data")

import app


def jpeg(color):
    stream = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(stream, format="JPEG")
    return stream.getvalue()


class CoreTests(unittest.TestCase):
    def setUp(self):
        app.init_db()
        with app.connect() as conn:
            conn.execute("DELETE FROM duplicate_reviews")
            conn.execute("DELETE FROM similarity_candidates")
            conn.execute("DELETE FROM similarity_fingerprints")
            conn.execute("DELETE FROM archive_images")
            conn.execute("DELETE FROM scan_issues")
            conn.execute("DELETE FROM comics")
            conn.execute("DELETE FROM roots")
        with app.duplicate_jobs_lock:
            app.duplicate_jobs.clear()
        self.root = TEST_WORKSPACE / uuid.uuid4().hex
        self.root.mkdir()
        self.archive = self.root / "測試作品 第02集.cbz"
        with zipfile.ZipFile(self.archive, "w") as z:
            z.writestr("001.jpg", jpeg("red"))
            z.writestr("002.jpg", jpeg("green"))
            z.writestr("note.txt", "metadata")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_scan_indexes_without_modifying_archive(self):
        before = self.archive.read_bytes()
        result = app.scan_directory(self.root)
        self.assertEqual(result["found"], 1)
        self.assertEqual(before, self.archive.read_bytes())
        with app.connect() as conn:
            row = conn.execute("SELECT image_count,volume_guess FROM comics WHERE path=?", (str(self.archive),)).fetchone()
        self.assertEqual(row["image_count"], 2)
        self.assertEqual(row["volume_guess"], "02")

    def test_scan_preserves_archive_filesystem_modified_time(self):
        original_modified = 946684800
        os.utime(self.archive, (original_modified, original_modified))

        app.scan_directory(self.root)
        app.scan_directory(self.root)

        with app.connect() as conn:
            row = conn.execute(
                "SELECT modified_at,last_seen FROM comics WHERE path=?", (str(self.archive),)
            ).fetchone()
        self.assertEqual(row["modified_at"], original_modified)
        self.assertGreater(row["last_seen"], original_modified)

    def test_duplicate_job_finishes_in_background_and_waits_for_acknowledgement(self):
        expected = {"calculated": 2, "groups": [{"sha256": "test", "files": [], "verdict": ""}]}
        with mock.patch.object(app, "calculate_duplicates", return_value=expected):
            started = app.start_duplicate_job()
            deadline = time.monotonic() + 2
            latest = app.latest_duplicate_job()
            while latest["status"] in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.01)
                latest = app.latest_duplicate_job()

        self.assertEqual(started["id"], latest["id"])
        self.assertEqual(latest["status"], "completed")
        self.assertEqual(latest["result"], expected)
        self.assertFalse(latest["acknowledged"])

        acknowledged = app.acknowledge_duplicate_job(latest["id"])
        self.assertTrue(acknowledged["acknowledged"])

    def test_scan_continues_after_unreadable_filesystem_entry(self):
        unreadable = self.root / "unreadable.zip"
        unreadable.write_bytes(b"broken")
        original_is_file = Path.is_file

        def guarded_is_file(path):
            if path == unreadable:
                raise OSError(1392, "file or directory is corrupted and unreadable")
            return original_is_file(path)

        with mock.patch.object(Path, "is_file", guarded_is_file):
            result = app.scan_directory(self.root)

        self.assertEqual(result["found"], 1)
        self.assertEqual(result["skipped"], 1)
        with app.connect() as conn:
            comic_count = conn.execute("SELECT COUNT(*) FROM comics WHERE status='available'").fetchone()[0]
            issue = conn.execute("SELECT path,kind FROM scan_issues").fetchone()
        self.assertEqual(comic_count, 1)
        self.assertEqual(issue["path"], str(unreadable))
        self.assertEqual(issue["kind"], "filesystem")

    def test_full_integrity_check_detects_decodable_image_failure(self):
        damaged = self.root / "image-damaged.zip"
        with zipfile.ZipFile(damaged, "w") as archive:
            archive.writestr("001.jpg", b"this is not a real image")

        scan_result = app.scan_directory(self.root)
        self.assertEqual(scan_result["failed"], 0)
        with app.connect() as conn:
            before = conn.execute("SELECT status FROM comics WHERE path=?", (str(damaged),)).fetchone()
        self.assertEqual(before["status"], "available")

        result = app.check_archive_integrity(app.IntegrityCheckRequest(path=str(self.root)))
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["failed"], 1)
        with app.connect() as conn:
            after = conn.execute("SELECT status,error FROM comics WHERE path=?", (str(damaged),)).fetchone()
        self.assertEqual(after["status"], "error")
        self.assertIn("001.jpg", after["error"])

    def test_rebuild_reorders_renames_and_preserves_non_image(self):
        app.rebuild_archive(self.archive, [
            {"source": "002.jpg", "name": "010.jpg", "deleted": False},
            {"source": "001.jpg", "name": "001.jpg", "deleted": False},
        ])
        with zipfile.ZipFile(self.archive) as z:
            images = [x.filename for x in z.infolist() if app.is_image_name(x.filename)]
            self.assertEqual(images, ["010.jpg", "001.jpg"])
            self.assertEqual(z.read("note.txt"), b"metadata")

    def test_rebuild_rejects_duplicate_target_names(self):
        original = self.archive.read_bytes()
        with self.assertRaises(ValueError):
            app.rebuild_archive(self.archive, [
                {"source": "001.jpg", "name": "same.jpg", "deleted": False},
                {"source": "002.jpg", "name": "same.jpg", "deleted": False},
            ])
        self.assertEqual(original, self.archive.read_bytes())

    def test_author_guess_from_common_filename(self):
        path = Path("(C100) [範例社團 (作者名)] 作品 [中文翻譯].zip")
        self.assertEqual(app.parse_author_guess(path), "作者名")

    def test_scanned_author_is_marked_as_guessed_and_manual_edit_is_preserved(self):
        guessed_archive = self.root / "(C100) [範例社團 (作者名)] 作品.zip"
        with zipfile.ZipFile(guessed_archive, "w") as archive:
            archive.writestr("001.jpg", jpeg("blue"))
        app.scan_directory(self.root)
        with app.connect() as conn:
            row = conn.execute(
                "SELECT id,author,author_source FROM comics WHERE path=?", (str(guessed_archive),)
            ).fetchone()
        self.assertEqual((row["author"], row["author_source"]), ("作者名", "guessed"))

        app.update_metadata(row["id"], app.MetadataRequest(author="正確作者", group_name=""))
        app.scan_directory(self.root)
        with app.connect() as conn:
            updated = conn.execute(
                "SELECT author,author_source FROM comics WHERE id=?", (row["id"],)
            ).fetchone()
        self.assertEqual((updated["author"], updated["author_source"]), ("正確作者", "manual"))

    def test_perceptual_hash_is_stable_for_same_image(self):
        data = jpeg("blue")
        self.assertEqual(app.perceptual_hash(data), app.perceptual_hash(data))

    def test_natural_page_order(self):
        names = ["10.jpg", "2.jpg", "001.jpg", "page20.jpg", "page3.jpg"]
        self.assertEqual(
            sorted(names, key=app.natural_sort_key),
            ["001.jpg", "2.jpg", "10.jpg", "page3.jpg", "page20.jpg"],
        )

    def test_thumbnail_is_smaller_preview(self):
        app.scan_directory(self.root)
        with app.connect() as conn:
            comic_id = conn.execute("SELECT id FROM comics WHERE path=?", (str(self.archive),)).fetchone()[0]
        response = app.archive_thumbnail(comic_id, "001.jpg", width=80)
        with Image.open(io.BytesIO(response.body)) as thumbnail:
            self.assertEqual(thumbnail.format, "JPEG")
            self.assertLessEqual(thumbnail.width, 80)

    def test_apply_preserves_staged_order_in_database(self):
        app.scan_directory(self.root)
        with app.connect() as conn:
            comic_id = conn.execute("SELECT id FROM comics WHERE path=?", (str(self.archive),)).fetchone()[0]
        app.apply_edits(comic_id, app.ApplyRequest(
            items=[
                app.EditItem(source="002.jpg", name="002.jpg"),
                app.EditItem(source="001.jpg", name="001.jpg"),
            ],
            confirmation="確認",
        ))
        detail = app.comic_detail(comic_id)
        self.assertEqual([item["name"] for item in detail["images"]], ["002.jpg", "001.jpg"])

    def test_wnacg_album_parser_preserves_source_order(self):
        index_html = '<h2>[作者] 測試作品</h2><label>頁數：3P</label>'
        item_script = 'mReader.initData({"page_url":["http://img.example/003.webp","http://img.example/001.webp","http://img.example/002.webp"]});'
        result = app.extract_wnacg_album(index_html, item_script, "https://www.wnacg.com/example")
        self.assertEqual(result["title"], "[作者] 測試作品")
        self.assertEqual(result["page_count"], 3)
        self.assertEqual(
            result["image_urls"],
            ["http://img.example/003.webp", "http://img.example/001.webp", "http://img.example/002.webp"],
        )

    def test_wnacg_album_parser_preserves_signed_query_parameters(self):
        index_html = '<h2>[作者] 驗證網址作品</h2><label>頁數：2P</label>'
        item_script = (
            'mReader.initData({"page_url":['
            '"http://img.example/001.webp?verify=123-first",'
            '"http://img.example/002.jpg?verify=456-second"'
            ']});'
        )
        result = app.extract_wnacg_album(
            index_html, item_script, "https://www.wnacg.com/example"
        )
        self.assertEqual(
            result["image_urls"],
            [
                "http://img.example/001.webp?verify=123-first",
                "http://img.example/002.jpg?verify=456-second",
            ],
        )

    def test_wnacg_album_parser_extracts_source_category_and_tags(self):
        index_html = (
            '<h2>標籤測試作品</h2><label>分類：同人誌／漢化</label><label>頁數：2P</label>'
            '<div class="addtags">標籤：'
            '<a class="tagshow">作者</a><a class="tagshow">泳裝</a>'
            '<a class="tagshow">作者</a></div>'
        )
        item_script = 'mReader.initData({"page_url":["http://img.example/001.webp","http://img.example/002.webp"]});'
        result = app.extract_wnacg_album(index_html, item_script, "https://www.wnacg.com/example")
        self.assertEqual(result["source_category"], "同人誌／漢化")
        self.assertEqual(result["source_tags"], ["作者", "泳裝"])

    def test_wnacg_album_parser_rejects_missing_pages(self):
        with self.assertRaisesRegex(ValueError, "頁數驗證失敗"):
            app.extract_wnacg_album(
                '<h2>缺頁作品</h2><label>頁數：4P</label>',
                'mReader.initData({"page_url":["http://img.example/001.webp","http://img.example/002.webp"]});',
                "https://www.wnacg.com/example",
            )

    def test_wnacg_album_parser_accepts_stale_count_one_above_manifest(self):
        result = app.extract_wnacg_album(
            '<h2>頁數標示多一頁</h2><label>頁數：3P</label>',
            'mReader.initData({"page_url":["http://img.example/001.webp","http://img.example/002.webp"]});',
            "https://www.wnacg.com/example",
        )
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["reported_page_count"], 3)
        self.assertTrue(result["page_count_adjusted"])

    def test_wnacg_album_parser_accepts_stale_count_one_below_manifest(self):
        result = app.extract_wnacg_album(
            '<h2>頁數標示少一頁</h2><label>頁數：2P</label>',
            'mReader.initData({"page_url":["http://img.example/001.webp","http://img.example/002.webp","http://img.example/003.webp"]});',
            "https://www.wnacg.com/example",
        )
        self.assertEqual(result["page_count"], 3)
        self.assertEqual(result["reported_page_count"], 2)
        self.assertTrue(result["page_count_adjusted"])

    def test_wnacg_url_and_archive_name_are_sanitized(self):
        normalized, origin, aid = app.normalize_wnacg_album_url(
            "https://www.wnacg.com/photos-index-page-7-aid-376334.html"
        )
        self.assertEqual(aid, "376334")
        self.assertEqual(origin, "https://www.wnacg.com")
        self.assertEqual(normalized, "https://www.wnacg.com/photos-index-page-1-aid-376334.html")
        self.assertEqual(app.safe_download_stem('作品:第六集?'), "作品_第六集_")

    def test_wnacg_series_parser_reads_dynamic_chapters_from_sibling_block(self):
        # WNACG currently closes #sr_pub before the sibling .sr_compact block.
        # The parser must therefore use the chapter data attributes rather than
        # relying on a parent-child relationship or a hard-coded chapter count.
        index_html = """
        <h2>[作者] 合輯測試</h2>
        <div id="sr_pub" data-aid="390766" data-order="asc" data-mode="list"></div>
        <div class="sr_compact">
          <a class="tagshow" data-chid="225339"
             href="/photos-slide-aid-225339-sid-390766.html"
             title="第1話 測試內容 31P">第1話 測試內容</a>
          <a class="tagshow" data-chid="234576"
             href="/photos-slide-aid-234576-sid-390766.html"
             title="第2話 測試內容 42P">第2話 測試內容</a>
        </div>
        <div class="bot_toolbar"><span class="sr_count">共 2 話</span></div>
        """
        result = app.parse_wnacg_series_page(
            index_html,
            "https://www.wnacg.com/photos-index-page-1-aid-390766.html",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "series")
        self.assertEqual(result["series_id"], "390766")
        self.assertEqual(result["reported_chapter_count"], 2)
        self.assertEqual(
            [(item["number"], item["aid"], item["page_count"]) for item in result["chapters"]],
            [(1, "225339", 31), (2, "234576", 42)],
        )

    def test_wnacg_chapter_url_keeps_series_id_when_normalized(self):
        normalized, origin, aid = app.normalize_wnacg_album_url(
            "https://www.wnacg.com/photos-slide-aid-225339-sid-390766.html"
        )
        self.assertEqual(aid, "225339")
        self.assertEqual(origin, "https://www.wnacg.com")
        self.assertEqual(
            normalized,
            "https://www.wnacg.com/photos-index-page-1-aid-225339-sid-390766.html",
        )

    def test_wnacg_series_parser_follows_directory_pages(self):
        page_one = """
        <h2>分頁合輯</h2>
        <div id="sr_pub" data-aid="99" data-order="asc" data-mode="list"></div>
        <div class="sr_compact">
          <a data-chid="101" href="/photos-slide-aid-101-sid-99.html" title="第1話 10P">第1話</a>
        </div>
        <a href="/photos-index-page-1-aid-99.html">1</a>
        <a href="/photos-index-page-2-aid-99.html">2</a>
        <span>共 3 話</span>
        """
        page_two = """
        <h2>分頁合輯</h2>
        <div id="sr_pub" data-aid="99" data-order="asc" data-mode="list"></div>
        <div class="sr_compact">
          <a data-chid="102" href="/photos-slide-aid-102-sid-99.html" title="第2話 11P">第2話</a>
          <a data-chid="103" href="/photos-slide-aid-103-sid-99.html" title="第3話 12P">第3話</a>
        </div>
        <a href="/photos-index-page-1-aid-99.html">1</a>
        <a href="/photos-index-page-2-aid-99.html">2</a>
        <span>共 3 話</span>
        """
        with mock.patch.object(app, "fetch_remote_bytes", return_value=page_two.encode("utf-8")) as fetch:
            result = app.inspect_wnacg_series(
                "https://www.wnacg.com/photos-index-aid-99.html",
                first_html=page_one,
            )
        self.assertEqual([item["aid"] for item in result["chapters"]], ["101", "102", "103"])
        self.assertEqual(result["chapter_count"], 3)
        fetch.assert_called_once_with("https://www.wnacg.com/photos-index-page-2-aid-99.html")

    def test_batch_preview_expands_series_into_one_item_per_chapter(self):
        with app.connect() as conn:
            conn.execute("INSERT INTO roots(path,created_at) VALUES(?,?)", (str(self.root), app.now_ts()))
        series = {
            "kind": "series",
            "title": "合輯測試",
            "series_id": "390766",
            "chapter_count": 2,
            "chapters": [
                {"number": 1, "aid": "225339", "title": "第1話", "url": "https://www.wnacg.com/photos-slide-aid-225339-sid-390766.html", "page_count": 31},
                {"number": 2, "aid": "234576", "title": "第2話", "url": "https://www.wnacg.com/photos-slide-aid-234576-sid-390766.html", "page_count": 42},
            ],
            "source_url": "https://www.wnacg.com/photos-index-aid-390766.html",
            "source_tags": [],
        }
        request = app.WebDownloadBatchPreviewRequest(
            urls=["https://www.wnacg.com/photos-index-aid-390766.html"],
            root_path=str(self.root),
        )
        with mock.patch.object(app, "inspect_wnacg_album", return_value=series):
            result = app.preview_web_download_batch(request)
        self.assertEqual(result["ready_count"], 2)
        self.assertEqual([item["chapter_id"] for item in result["items"]], ["225339", "234576"])
        self.assertEqual([item["chapter_number"] for item in result["items"]], [1, 2])
        self.assertEqual([item["page_count"] for item in result["items"]], [31, 42])
        self.assertEqual(
            [item["archive_name"] for item in result["items"]],
            ["第1話.zip", "第2話.zip"],
        )

    def test_series_preview_reconnects_existing_archives_and_reader_directory(self):
        chapter_paths = [self.root / "第1話.zip", self.root / "第2話.zip"]
        for path, color in zip(chapter_paths, ("red", "blue")):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("001.jpg", jpeg(color))
        app.scan_directory(self.root)
        with app.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO roots(path,created_at) VALUES(?,?)", (str(self.root), app.now_ts()))
        series = {
            "kind": "series", "title": "合輯測試", "series_id": "390766", "chapter_count": 2,
            "chapters": [
                {"number": 1, "aid": "225339", "title": "第1話", "url": "https://www.wnacg.com/photos-slide-aid-225339-sid-390766.html", "page_count": 1},
                {"number": 2, "aid": "234576", "title": "第2話", "url": "https://www.wnacg.com/photos-slide-aid-234576-sid-390766.html", "page_count": 1},
            ],
            "source_url": "https://www.wnacg.com/photos-index-aid-390766.html", "source_tags": [],
        }
        request = app.WebDownloadBatchPreviewRequest(
            urls=["https://www.wnacg.com/photos-index-aid-390766.html"], root_path=str(self.root)
        )
        with mock.patch.object(app, "inspect_wnacg_album", return_value=series):
            result = app.preview_web_download_batch(request)

        self.assertEqual([item["status"] for item in result["items"]], ["paused", "paused"])
        self.assertTrue(all(item.get("comic_id") for item in result["items"]))
        first_detail = app.comic_detail(result["items"][0]["comic_id"])
        self.assertEqual(first_detail["series_title"], "合輯測試")
        self.assertEqual(
            [(chapter["chapter_number"], chapter["current"]) for chapter in first_detail["chapters"]],
            [(1, 1), (2, 0)],
        )

    def test_download_archive_keeps_numeric_manifest_order(self):
        pages_dir = self.root / "download-pages"
        pages_dir.mkdir()
        pages = []
        for number, color in [(1, "red"), (2, "green"), (3, "blue")]:
            page = pages_dir / f"{number:03d}.jpg"
            page.write_bytes(jpeg(color))
            pages.append(page)
        target = self.root / "download.zip"
        app.create_download_archive(target, pages)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.namelist(), ["001.jpg", "002.jpg", "003.jpg"])

    def test_http_429_explains_the_failure_in_plain_language(self):
        error = app.HTTPError("https://img.example/1.jpg", 429, "Too Many Requests", {}, None)
        with mock.patch.object(app, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "請求太頻繁"):
                app.fetch_remote_bytes("https://img.example/1.jpg")

    def test_failed_web_download_leaves_no_archive_or_temp_files(self):
        job_id = uuid.uuid4().hex
        app.download_jobs[job_id] = {
            "id": job_id, "status": "queued", "title": "", "total": 0, "completed": 0,
            "message": "", "error": "", "archive_path": "", "created_at": app.now_ts(),
            "finished_at": None, "_cancel_event": threading.Event(), "_user_cancelled": False,
        }
        album = {
            "title": "失敗測試作品", "page_count": 3,
            "image_urls": ["http://img.example/001.jpg", "http://img.example/002.jpg", "http://img.example/003.jpg"],
            "source_url": "https://www.wnacg.com/example",
        }

        def fake_download(_job_id, page_number, _url, output_path, _referer, _deadline, _cancel_event):
            if page_number == 2:
                raise RuntimeError("模擬連線失敗")
            output_path.write_bytes(jpeg("red"))

        with mock.patch.object(app, "inspect_wnacg_album", return_value=album), mock.patch.object(
            app, "download_one_page", side_effect=fake_download
        ):
            app.run_download_job(job_id, "https://www.wnacg.com/example-aid-1.html", self.root)

        self.assertEqual(app.download_jobs[job_id]["status"], "failed")
        self.assertIn("第 2 頁下載失敗", app.download_jobs[job_id]["message"])
        self.assertFalse((self.root / "失敗測試作品.zip").exists())
        self.assertEqual(list(self.root.glob(".comic-download-*")), [])
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_completed_web_download_applies_selected_source_tags(self):
        job_id = uuid.uuid4().hex
        app.download_jobs[job_id] = {
            "id": job_id, "status": "queued", "title": "", "total": 0, "completed": 0,
            "message": "", "error": "", "archive_path": "", "created_at": app.now_ts(),
            "finished_at": None, "selected_tags": ["自訂保留", "泳裝"], "group_name": "合輯分組測試",
            "_cancel_event": threading.Event(), "_user_cancelled": False,
        }
        album = {
            "title": "標籤下載測試作品", "page_count": 2,
            "image_urls": ["http://img.example/001.jpg", "http://img.example/002.jpg"],
            "source_url": "https://www.wnacg.com/example",
            "source_category": "同人誌／漢化", "source_tags": ["來源標籤", "泳裝"],
        }

        def fake_download(_job_id, _page_number, _url, output_path, _referer, _deadline, _cancel_event):
            output_path.write_bytes(jpeg("red"))

        with mock.patch.object(app, "inspect_wnacg_album", return_value=album), mock.patch.object(
            app, "download_one_page", side_effect=fake_download
        ):
            app.run_download_job(job_id, "https://www.wnacg.com/example-aid-1.html", self.root)

        self.assertEqual(app.download_jobs[job_id]["status"], "completed")
        with app.connect() as conn:
            comic_id = conn.execute(
                "SELECT id FROM comics WHERE path=?", (str(self.root / "標籤下載測試作品.zip"),)
            ).fetchone()[0]
            group_name = conn.execute("SELECT group_name FROM comics WHERE id=?", (comic_id,)).fetchone()[0]
            tags = [row[0] for row in conn.execute(
                "SELECT tags.name FROM comic_tags JOIN tags ON tags.id=comic_tags.tag_id WHERE comic_id=? ORDER BY tags.name",
                (comic_id,),
            )]
        self.assertEqual(group_name, "合輯分組測試")
        self.assertEqual(tags, ["泳裝", "自訂保留"])

    def test_series_chapter_job_uses_chapter_inspector(self):
        job_id = uuid.uuid4().hex
        app.download_jobs[job_id] = {
            "id": job_id, "status": "queued", "title": "", "total": 0, "completed": 0,
            "message": "", "error": "", "archive_path": "", "created_at": app.now_ts(),
            "finished_at": None, "group_name": "合輯測試", "series_title": "合輯測試",
            "series_id": "390766", "chapter_id": "225339", "chapter_number": 1,
            "chapter_count": 2, "_cancel_event": threading.Event(), "_user_cancelled": False,
        }
        album = {
            "title": "合輯章節測試", "page_count": 1,
            "image_urls": ["http://img.example/001.jpg"],
            "source_url": "https://www.wnacg.com/photos-index-page-1-aid-225339-sid-390766.html",
            "aid": "225339", "series_id": "390766",
        }

        def fake_download(_job_id, _page_number, _url, output_path, _referer, _deadline, _cancel_event):
            output_path.write_bytes(jpeg("purple"))

        child_url = "https://www.wnacg.com/photos-slide-aid-225339-sid-390766.html"
        with mock.patch.object(app, "inspect_wnacg_chapter", return_value=album) as inspect_chapter, mock.patch.object(
            app, "inspect_wnacg_album", side_effect=AssertionError("series child must not inspect parent")
        ), mock.patch.object(app, "download_one_page", side_effect=fake_download):
            app.run_download_job(job_id, child_url, self.root)

        inspect_chapter.assert_called_once_with(child_url)
        self.assertEqual(app.download_jobs[job_id]["status"], "completed")
        with app.connect() as conn:
            comic = conn.execute(
                "SELECT source_series_id,source_chapter_id,chapter_number,chapter_count,series_title "
                "FROM comics WHERE path=?",
                (str(self.root / "合輯章節測試.zip"),),
            ).fetchone()
        self.assertEqual(
            tuple(comic), ("390766", "225339", 1, 2, "合輯測試")
        )

    def test_cancelled_web_download_can_be_retried(self):
        job_id = uuid.uuid4().hex
        app.download_jobs[job_id] = {
            "id": job_id, "status": "cancelled", "title": "取消測試", "total": 3, "completed": 1,
            "message": "下載已由使用者取消", "error": "", "archive_path": "",
            "request_url": "https://www.wnacg.com/example-aid-1.html", "root_path": str(self.root),
            "created_at": app.now_ts(), "finished_at": app.now_ts(), "retry_count": 0,
            "cancel_requested": True, "_cancel_event": threading.Event(), "_user_cancelled": True,
        }
        with mock.patch.object(app, "launch_download_job") as launch:
            result = app.retry_web_download(job_id)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["retry_count"], 1)
        launch.assert_called_once_with(job_id)

    def test_finished_web_download_record_can_be_deleted(self):
        job_id = uuid.uuid4().hex
        temp_dir = self.root / f".comic-download-{job_id}"
        temp_dir.mkdir()
        app.download_jobs[job_id] = {
            "id": job_id, "status": "failed", "root_path": str(self.root), "archive_name": "失敗.zip",
        }

        self.assertEqual(app.delete_web_download(job_id), {"deleted": True})
        self.assertNotIn(job_id, app.download_jobs)
        self.assertFalse(temp_dir.exists())

    def test_active_web_download_record_cannot_be_deleted(self):
        job_id = uuid.uuid4().hex
        app.download_jobs[job_id] = {"id": job_id, "status": "running"}

        with self.assertRaises(app.HTTPException) as raised:
            app.delete_web_download(job_id)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn(job_id, app.download_jobs)
        app.download_jobs.pop(job_id, None)

    def test_batch_preview_pauses_existing_and_in_batch_duplicates(self):
        with app.connect() as conn:
            conn.execute("INSERT INTO roots(path,created_at) VALUES(?,?)", (str(self.root), app.now_ts()))
        (self.root / "已存在作品.zip").write_bytes(b"existing")
        albums = {
            "aid-1": {
                "title": "新作品", "page_count": 10, "image_urls": ["https://img/1.jpg"] * 10,
                "source_url": "https://www.wnacg.com/photos-index-page-1-aid-1.html",
            },
            "aid-2": {
                "title": "已存在作品", "page_count": 8, "image_urls": ["https://img/2.jpg"] * 8,
                "source_url": "https://www.wnacg.com/photos-index-page-1-aid-2.html",
            },
        }

        def fake_inspect(url):
            return albums["aid-1" if "aid-1" in url else "aid-2"]

        request = app.WebDownloadBatchPreviewRequest(
            urls=[
                "https://www.wnacg.com/photos-index-page-1-aid-1.html",
                "https://www.wnacg.com/photos-index-page-1-aid-1.html",
                "https://www.wnacg.com/photos-index-page-1-aid-2.html",
            ],
            root_path=str(self.root),
        )
        with mock.patch.object(app, "inspect_wnacg_album", side_effect=fake_inspect):
            result = app.preview_web_download_batch(request)

        self.assertEqual([item["status"] for item in result["items"]], ["ready", "paused", "paused"])
        self.assertEqual(result["ready_count"], 1)
        self.assertEqual(result["paused_count"], 2)

    def test_batch_preview_ignores_missing_and_deleted_database_records(self):
        with app.connect() as conn:
            conn.execute("INSERT INTO roots(path,created_at) VALUES(?,?)", (str(self.root), app.now_ts()))
            for status in ("missing", "deleted"):
                ghost = self.root / f"不存在-{status}.zip"
                conn.execute(
                    "INSERT INTO comics(path,name,extension,size_bytes,modified_at,status,last_seen) VALUES(?,?,?,?,?,?,?)",
                    (str(ghost), ghost.name, ".zip", 0, 0, status, app.now_ts()),
                )
        album = {
            "title": "不存在-missing", "page_count": 2,
            "image_urls": ["https://img/1.jpg", "https://img/2.jpg"],
            "source_url": "https://www.wnacg.com/photos-index-page-1-aid-9.html",
        }
        request = app.WebDownloadBatchPreviewRequest(
            urls=["https://www.wnacg.com/photos-index-page-1-aid-9.html"],
            root_path=str(self.root),
        )
        with mock.patch.object(app, "inspect_wnacg_album", return_value=album):
            result = app.preview_web_download_batch(request)
        self.assertEqual(result["items"][0]["status"], "ready")

    def test_split_coauthors(self):
        self.assertEqual(app.split_author_labels("作者甲, 作者乙、作者丙"), ["作者甲", "作者乙", "作者丙"])

    def test_similar_results_are_cached_and_delete_only_removes_affected_candidate(self):
        duplicate = self.root / "測試作品 副本.cbz"
        shutil.copyfile(self.archive, duplicate)
        app.scan_directory(self.root)

        first = app.calculate_similar_content()
        self.assertEqual(first["recalculated"], 2)
        self.assertEqual(len(first["matches"]), 1)
        self.assertGreaterEqual(first["matches"][0]["left"]["modified_at"], first["matches"][0]["right"]["modified_at"])

        second = app.calculate_similar_content()
        self.assertEqual(second["recalculated"], 0)
        self.assertEqual(len(second["matches"]), 1)

        selected_ids = [second["matches"][0]["left"]["id"], second["matches"][0]["right"]["id"]]
        selected = app.calculate_selected_similar_content(app.SimilarGroupRequest(comic_ids=selected_ids))
        self.assertEqual(selected["checked"], 2)
        self.assertEqual(len(selected["matches"]), 1)
        self.assertEqual(
            {selected["matches"][0]["left"]["id"], selected["matches"][0]["right"]["id"]},
            set(selected_ids),
        )

        deleted_id = second["matches"][0]["left"]["id"]
        app.delete_comic(deleted_id, app.FileActionRequest(value="", confirmation="confirm-delete"))
        after_delete = app.calculate_similar_content()
        self.assertEqual(after_delete["recalculated"], 0)
        self.assertEqual(after_delete["matches"], [])
        with app.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM similarity_candidates").fetchone()[0], 0)

    def test_exact_duplicate_files_are_newest_first(self):
        duplicate = self.root / "測試作品 較新副本.cbz"
        shutil.copyfile(self.archive, duplicate)
        app.scan_directory(self.root)
        with app.connect() as conn:
            conn.execute("UPDATE comics SET modified_at=100 WHERE path=?", (str(self.archive),))
            conn.execute("UPDATE comics SET modified_at=200 WHERE path=?", (str(duplicate),))

        result = app.calculate_duplicates()
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual([file["modified_at"] for file in result["groups"][0]["files"]], [200, 100])
        self.assertEqual(result["groups"][0]["files"][0]["path"], str(duplicate))

    def test_duplicate_content_is_found_when_archive_sizes_differ(self):
        duplicate = self.root / "測試作品 不同壓縮.cbz"
        with zipfile.ZipFile(duplicate, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("001.jpg", jpeg("red"))
            archive.writestr("002.jpg", jpeg("green"))
            archive.writestr("note.txt", "different archive metadata" * 200)
        app.scan_directory(self.root)
        self.assertNotEqual(self.archive.stat().st_size, duplicate.stat().st_size)

        result = app.calculate_duplicates()

        self.assertEqual(result["groups"], [])
        self.assertEqual(len(result["similar_matches"]), 1)
        self.assertEqual(result["similar_matches"][0]["left"]["image_count"], 2)
        self.assertEqual(result["similar_matches"][0]["right"]["image_count"], 2)

    def test_ambiguous_filename_numbers_are_not_guessed_as_volume(self):
        title, volume = app.parse_filename(Path("作品 2026 001.zip"))
        self.assertEqual(volume, "")

    def test_exact_duplicate_check_excludes_file_deleted_outside_app(self):
        duplicate = self.root / "測試作品 外部刪除副本.cbz"
        shutil.copyfile(self.archive, duplicate)
        app.scan_directory(self.root)
        self.assertEqual(len(app.calculate_duplicates()["groups"]), 1)

        duplicate.unlink()
        result = app.calculate_duplicates()

        self.assertEqual(result["groups"], [])
        with app.connect() as conn:
            status = conn.execute("SELECT status FROM comics WHERE path=?", (str(duplicate),)).fetchone()[0]
        self.assertEqual(status, "missing")

    def test_delete_invalidates_completed_background_duplicate_result(self):
        duplicate = self.root / "測試作品 背景結果副本.cbz"
        shutil.copyfile(self.archive, duplicate)
        app.scan_directory(self.root)
        result = app.calculate_duplicates()
        with app.duplicate_jobs_lock:
            app.duplicate_jobs["completed-job"] = {
                "id": "completed-job", "status": "completed", "message": "完成", "error": "",
                "result": result, "created_at": app.now_ts(), "finished_at": app.now_ts(),
                "acknowledged": True, "stale": False,
            }
        with app.connect() as conn:
            duplicate_id = conn.execute("SELECT id FROM comics WHERE path=?", (str(duplicate),)).fetchone()[0]

        app.delete_comic(duplicate_id, app.FileActionRequest(value="", confirmation="confirm-delete"))

        self.assertEqual(app.latest_duplicate_job(), {"status": "idle"})

    def test_rescan_changed_archive_invalidates_sha256(self):
        app.scan_directory(self.root)
        with app.connect() as conn:
            conn.execute("UPDATE comics SET sha256='old-digest' WHERE path=?", (str(self.archive),))
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("003.jpg", jpeg("blue"))

        app.scan_directory(self.root)
        with app.connect() as conn:
            digest = conn.execute("SELECT sha256 FROM comics WHERE path=?", (str(self.archive),)).fetchone()[0]
        self.assertIsNone(digest)

    def test_comic_supports_multiple_groups_and_tags(self):
        app.scan_directory(self.root)
        with app.connect() as conn:
            comic_id = conn.execute("SELECT id FROM comics WHERE path=?", (str(self.archive),)).fetchone()[0]

        groups = [f"group-{uuid.uuid4().hex}", f"group-{uuid.uuid4().hex}"]
        tags = [f"tag-{uuid.uuid4().hex}", f"tag-{uuid.uuid4().hex}"]
        app.replace_comic_groups(comic_id, app.ComicGroupsRequest(groups=groups))
        app.replace_comic_tags(comic_id, app.ComicTagsRequest(tags=tags))

        detail = app.comic_detail(comic_id)
        self.assertEqual(set(detail["groups"]), set(groups))
        self.assertEqual(set(detail["tags"]), set(tags))

        by_group = app.comics(group=groups[1], tag="", q="", status="available", root="",
                              sort="modified_at", direction="desc")
        by_tag = app.comics(group="", tag=tags[1], q="", status="available", root="",
                            sort="modified_at", direction="desc")
        self.assertEqual([item["id"] for item in by_group["items"]], [comic_id])
        self.assertEqual([item["id"] for item in by_tag["items"]], [comic_id])

    def test_standalone_reader_stays_read_only_and_does_not_save_page(self):
        reader_html = (PROJECT_DIR / "static" / "reader.html").read_text(encoding="utf-8")
        reader_script = (PROJECT_DIR / "static" / "reader.js").read_text(encoding="utf-8")

        self.assertIn('id="readerApp"', reader_html)
        self.assertIn('/reader.js', reader_html)
        self.assertIn("new URLSearchParams(window.location.search).get('comic')", reader_script)
        self.assertNotIn("get('page')", reader_script)
        self.assertNotIn("localStorage", reader_script)
        self.assertNotIn("sessionStorage", reader_script)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertNotIn(f"method: '{method}'", reader_script)

    def test_start_reading_is_a_real_new_tab_link(self):
        enhancements = (PROJECT_DIR / "static" / "enhancements.js").read_text(encoding="utf-8")
        app_script = (PROJECT_DIR / "static" / "app.js").read_text(encoding="utf-8")
        index_html = (PROJECT_DIR / "static" / "index.html").read_text(encoding="utf-8")
        enhancements_css = (PROJECT_DIR / "static" / "enhancements.css").read_text(encoding="utf-8")

        self.assertIn('href="/reader.html?comic=${c.id}"', enhancements)
        self.assertIn('target="_blank" rel="noopener" data-read-once="new"', enhancements)
        self.assertIn('>▼</button>', enhancements)
        self.assertIn('id="manageComicImages"', enhancements)
        self.assertIn('id="openSettings"', index_html)
        self.assertIn('id="openReadingSetting"', index_html)
        self.assertIn('name="readingOpenMode" value="same"', index_html)
        self.assertIn('name="readingOpenMode" value="new"', index_html)
        self.assertIn('id="toggleSelection"', index_html)
        self.assertIn('state.selectionMode = false', enhancements)
        self.assertIn("#library').classList.toggle('selection-mode'", enhancements)
        self.assertIn('.reading-marker { display:none !important; }', enhancements_css)
        self.assertIn('#library.selection-mode .select-comic', enhancements_css)
        self.assertIn('.cover .format { left:auto; right:10px; top:auto; bottom:31px; }', enhancements_css)
        self.assertNotIn('所有檔案只在這台電腦處理', index_html)
        self.assertIn("$('#settingsDialog').addEventListener('cancel', showSettingsHome)", enhancements)
        self.assertIn('function updateReadingPositionCards(comicId)', enhancements)
        self.assertNotIn('🔖 上次看到這裡</span>', enhancements)
        self.assertNotIn('if(e.target.id==="readComic")startReader()', app_script)


if __name__ == "__main__":
    unittest.main()
