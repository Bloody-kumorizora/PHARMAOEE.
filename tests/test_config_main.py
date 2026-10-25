"""Пути к данным, настройки, проверка запуска и импорт базы v8."""
import hashlib
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pharmaoee_app import config, main
from pharmaoee_app.config import AppPaths, Settings, resolve_paths

from tests.helpers import TempDirTestCase, make_v8_db


class PathTests(TempDirTestCase):
    def test_priority_cli_env_default(self):
        with mock.patch.dict(os.environ, {config.ENV_DATA_DIR: str(self.tmp / "env")}):
            self.assertEqual(resolve_paths(str(self.tmp / "cli")).data_dir, (self.tmp / "cli").resolve())
            self.assertEqual(resolve_paths(None).data_dir, (self.tmp / "env").resolve())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(config.ENV_DATA_DIR, None)
            self.assertEqual(resolve_paths(None).data_dir, config.default_data_dir().resolve())

    def test_default_path_does_not_depend_on_working_directory(self):
        os.environ.pop(config.ENV_DATA_DIR, None)
        before = resolve_paths(None).db_path
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            self.assertEqual(resolve_paths(None).db_path, before)
        finally:
            os.chdir(cwd)
        self.assertNotEqual(before.parent, config.PROJECT_DIR)

    def test_relative_cli_path_is_made_absolute(self):
        self.assertTrue(resolve_paths("relative_dir").data_dir.is_absolute())


class SettingsTests(TempDirTestCase):
    def test_roundtrip_and_corrupted_file(self):
        path = self.tmp / "settings.json"
        s = Settings(oee_threshold_pct=72.5, default_source_filter="manual")
        s.save(path)
        loaded = Settings.load(path)
        self.assertEqual((loaded.oee_threshold_pct, loaded.default_source_filter), (72.5, "manual"))
        path.write_text("{ broken", encoding="utf-8")
        self.assertEqual(Settings.load(path).oee_threshold_pct, 60.0)
        path.write_text('{"oee_threshold_pct": 500, "default_source_filter": "hack"}', encoding="utf-8")
        loaded = Settings.load(path)
        self.assertEqual((loaded.oee_threshold_pct, loaded.default_source_filter), (60.0, "all"))
        self.assertEqual(Settings.load(self.tmp / "missing.json").oee_threshold_pct, 60.0)


class MainTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(config.shutdown_logging)

    def test_check_mode_on_empty_directory(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main.main(["--check", "--data-dir", str(self.tmp / "data")])
        self.assertEqual(code, 0)
        self.assertIn("проверка: ok", out.getvalue())
        self.assertTrue((self.tmp / "data" / "pharmaoee.db").exists())
        self.assertTrue((self.tmp / "data" / "logs" / "pharmaoee.log").exists())

    def test_legacy_database_is_copied_and_original_untouched(self):
        legacy = make_v8_db(self.tmp / "pharmaoee.db")
        digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
        paths = AppPaths(self.tmp / "data")
        paths.ensure()
        with mock.patch.object(main, "LEGACY_DB_PATH", legacy):
            db, notes = main.prepare_database(paths)
        self.assertEqual(db.count_production(), 3)
        self.assertTrue(any("предыдущей версии" in n for n in notes))
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), digest)
        # Повторный запуск не импортирует снова.
        with mock.patch.object(main, "LEGACY_DB_PATH", legacy):
            _, notes = main.prepare_database(paths)
        self.assertEqual(notes, [])

    def test_check_mode_reports_unknown_database(self):
        data = self.tmp / "data"
        data.mkdir()
        (data / "pharmaoee.db").write_text("garbage", encoding="utf-8")
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()) as err:
            code = main.main(["--check", "--data-dir", str(data)])
        self.assertEqual(code, 1)
        self.assertIn("ОШИБКА", err.getvalue())
        self.assertEqual((data / "pharmaoee.db").read_text(encoding="utf-8"), "garbage")


if __name__ == "__main__":
    unittest.main()
