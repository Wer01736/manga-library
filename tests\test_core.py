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


def jpeg(_color="white"):
    stream = io.BytesIO()
    Image.new("RGB", (24, 32), "white").save(stream, format="JPEG")
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


if __name__ == "__main__":
    unittest.main()
