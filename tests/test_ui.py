"""Интеграционные проверки интерфейса на реальном Tk (окно скрыто).

Если графического окружения нет (например, сервер без дисплея), тесты пропускаются
с понятным сообщением — это не считается успешной проверкой GUI.
"""
import time
import tkinter as tk
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from pharmaoee_app import demo, formatting
from pharmaoee_app.config import AppPaths, Settings
from pharmaoee_app.main import prepare_database

from tests.helpers import TempDirTestCase

try:
    _probe = tk.Tk()
    _probe.destroy()
    TK_ERROR = None
except tk.TclError as exc:          # нет дисплея
    TK_ERROR = str(exc)


@unittest.skipIf(TK_ERROR, f"Нет графического окружения: {TK_ERROR}")
class UITestCase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.messages = []
        patches = {
            "showinfo": lambda *a, **k: self.messages.append(("info", a)),
            "showwarning": lambda *a, **k: self.messages.append(("warning", a)),
            "showerror": lambda *a, **k: self.messages.append(("error", a)),
            "askyesno": lambda *a, **k: True,
        }
        for name, func in patches.items():
            patcher = mock.patch(f"tkinter.messagebox.{name}", side_effect=func)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.paths = AppPaths(self.tmp / "data")
        self.paths.ensure()
        self.db, notes = prepare_database(self.paths)
        from pharmaoee_app.ui.app import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.app = App(self.root, self.paths, self.db, Settings(), notes)
        self.root.update_idletasks()

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def pump(self, timeout=20.0):
        deadline = time.time() + timeout
        while self.app._busy and time.time() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()
        self.assertFalse(self.app._busy, "фоновая операция не завершилась")

    def errors(self):
        return [m for m in self.messages if m[0] == "error"]


class ViewTests(UITestCase):
    def test_all_views_open_on_empty_database(self):
        for key in self.app.views:
            self.app.show_view(key)
        self.root.update()
        self.assertEqual(self.app.views["dashboard"].tiles["oee"].value.cget("text"), "—")
        self.assertEqual(self.errors(), [])
        self.assertNotIn("Не удалось", self.app.status.message.cget("text"))

    def test_views_and_charts_with_demo_data(self):
        today = date.today()
        demo.generate(self.db, demo.DemoRequest(today - timedelta(days=20), today - timedelta(days=1),
                                                shifts=("1 смена", "2 смена", "3 смена")))
        self.app.set_period(today - timedelta(days=20), today - timedelta(days=1))
        snap = self.app.snapshot()
        for key in self.app.views:
            self.app.show_view(key)
        self.assertEqual(self.app.views["dashboard"].tiles["oee"].value.cget("text"), formatting.pct(snap.total.oee))
        self.assertEqual(len(self.app.views["production"].table.rows), len(snap.production))
        self.assertEqual(len(self.app.views["downtime"].table.rows), len(snap.downtimes))
        analytics_view = self.app.views["analytics"]
        charts = [self.app.views["dashboard"].trend, self.app.views["dashboard"].reasons_chart,
                  analytics_view.trend_chart, analytics_view.reasons_chart, analytics_view.equipment_chart,
                  analytics_view.shift_chart, analytics_view.output_chart, analytics_view.defect_chart]
        for chart in charts:
            self.assertTrue(chart.has_data(), type(chart).__name__)
            chart.delete("all")
            chart.draw(900, 300)          # реальная отрисовка на холсте без исключений
            chart.draw(320, 160)          # узкий размер
            self.assertTrue(chart.find_all())
        self.assertEqual(self.errors(), [])

    def test_selected_day_filter_applies_to_all_views(self):
        from pharmaoee_app.validation import EquipmentInput, ProductionInput
        eq = self.db.add_equipment(EquipmentInput("Линия 1", "", 1.0))
        self.db.add_production(ProductionInput(eq, date(2026, 10, 1), "1 смена", 480, 400, 20000, 19000, 1.0))
        self.db.add_production(ProductionInput(eq, date(2026, 10, 2), "1 смена", 480, 300, 10000, 9000, 1.0))
        self.app.data_changed()
        self.app.set_period(date(2026, 10, 1), date(2026, 10, 1))
        self.app.show_view("production")
        self.assertEqual([r["work_date"] for r in self.app.views["production"].table.rows], ["2026-10-01"])
        self.app.show_view("dashboard")
        self.assertEqual(self.app.views["dashboard"].tiles["oee"].value.cget("text"), formatting.pct(19000 / 28800))
        self.app.show_view("analytics")
        self.assertEqual([r["day"] for r in self.app.views["analytics"].trend_table.rows], ["2026-10-01"])

    def test_invalid_filter_date_shows_inline_error(self):
        self.app.f_from.var.set("31.02.2026")
        before = self.app.filters()
        self.app.apply_filters()
        self.assertIn("не существует", self.app.filter_error.cget("text"))
        self.assertEqual(self.app.filters(), before)
        self.app.f_from.var.set("05.10.2026")
        self.app.f_to.var.set("01.10.2026")
        self.app.apply_filters()
        self.assertIn("раньше начала", self.app.filter_error.cget("text"))


class DialogTests(UITestCase):
    def setUp(self):
        super().setUp()
        from pharmaoee_app.validation import EquipmentInput
        self.eq = self.db.add_equipment(EquipmentInput("Линия 1", "", 1.2))
        self.app.data_changed()

    def fill_production(self, dialog, **values):
        base = dict(work_date="20.10.2026", shift="1 смена", planned="480", run="420", total="18000", good="17640",
                    cycle="1,2", note="")
        base.update(values)
        dialog.picker.set_id(self.eq)
        for key, value in base.items():
            dialog.v[key].set(value)

    def test_production_dialog_saves_and_blocks_duplicate(self):
        from pharmaoee_app.ui.dialogs import ProductionDialog
        d = ProductionDialog(self.app)
        self.fill_production(d)
        self.assertEqual(d.preview_values["oee"].cget("text"), "73,5%")
        d.save()
        self.assertFalse(d.winfo_exists())
        self.assertEqual(self.db.count_production(), 1)
        d2 = ProductionDialog(self.app)
        self.fill_production(d2)
        d2.save()
        self.assertIn("уже существует", d2.error.cget("text"))
        self.assertEqual(self.db.count_production(), 1)
        d2.destroy()

    def test_production_dialog_validation_errors(self):
        from pharmaoee_app.ui.dialogs import ProductionDialog
        d = ProductionDialog(self.app)
        self.fill_production(d, run="500")
        d.save()
        self.assertIn("больше планового", d.error.cget("text"))
        self.assertEqual(self.db.count_production(), 0)
        d.destroy()

    def test_run_from_downtimes(self):
        from pharmaoee_app.ui.dialogs import ProductionDialog
        from pharmaoee_app.validation import DowntimeInput
        t = datetime(2026, 10, 20, 9, 0)
        self.db.add_downtime(DowntimeInput(self.eq, date(2026, 10, 20), "1 смена", t, t + timedelta(minutes=35),
                                           "Переналадка"))
        d = ProductionDialog(self.app)
        self.fill_production(d, run="")
        d.run_from_downtimes()
        self.assertEqual(d.v["run"].get(), "445")
        d.destroy()

    def test_downtime_dialog_midnight_and_overlap(self):
        from pharmaoee_app.ui.dialogs import DowntimeDialog
        d = DowntimeDialog(self.app)
        d.picker.set_id(self.eq)
        d.start_date.set_date(date(2026, 10, 20))
        d.v["start_time"].set("23:50")
        d.end_date.set_date(date(2026, 10, 20))
        d.v["end_time"].set("00:20")
        self.assertEqual((d.v["work_date"].get(), d.v["shift"].get()), ("20.10.2026", "3 смена"))
        d.save()
        self.assertIn("через полночь", d.error.cget("text"))
        d._end_next_day()
        self.assertEqual(d.duration.cget("text"), "30 мин")
        d.save()
        self.assertFalse(d.winfo_exists())
        d2 = DowntimeDialog(self.app)
        d2.picker.set_id(self.eq)
        d2.start_date.set_date(date(2026, 10, 21))
        d2.v["start_time"].set("00:00")
        d2.end_date.set_date(date(2026, 10, 21))
        d2.v["end_time"].set("00:30")
        d2.save()
        self.assertIn("пересекается", d2.error.cget("text"))
        d2.destroy()
        self.assertEqual(len(self.db.list_downtimes()), 1)

    def test_equipment_dialog_duplicate(self):
        from pharmaoee_app.ui.dialogs import EquipmentDialog
        d = EquipmentDialog(self.app)
        d.v["name"].set("линия 1")
        d.save()
        self.assertIn("уже есть", d.error.cget("text"))
        d.v["name"].set("Линия 2")
        d.v["cycle"].set("0,8")
        d.save()
        self.assertFalse(d.winfo_exists())
        self.assertEqual(len(self.db.list_equipment()), 2)


class TableTests(UITestCase):
    def test_sort_and_search(self):
        from pharmaoee_app.ui.widgets import Column, DataTable
        table = DataTable(self.root, [Column("name", "Имя"), Column("value", "Значение", numeric=True)])
        table.set_rows([{"id": 1, "name": "Бета", "value": 10}, {"id": 2, "name": "альфа", "value": 2},
                        {"id": 3, "name": "Гамма", "value": None}])
        table.sort_by("value")
        self.assertEqual([r["id"] for r in table.visible], [2, 1, 3])     # пустые значения в конце
        table.sort_by("value")
        self.assertEqual([r["id"] for r in table.visible], [1, 2, 3])
        table.sort_by("name")
        self.assertEqual([r["name"] for r in table.visible], ["альфа", "Бета", "Гамма"])
        table.search("гам")
        self.assertEqual([r["id"] for r in table.visible], [3])
        table.search("нет такого")
        self.assertEqual(table.visible, [])


class ServiceViewTests(UITestCase):
    def test_simulator_session_creates_shift_record(self):
        view = self.app.views["demo"]
        self.app.show_view("demo")
        view.prepare_equipment()
        view.refresh()
        self.assertFalse(view.sim_picker.is_empty)
        view.sim_date.set_date(date.today() - timedelta(days=1))
        from pharmaoee_app.ui.views_service import SPEEDS
        view.sim_speed.set(list(SPEEDS)[0])
        view.start_sim()
        guard = 0
        while view.sim is not None and guard < 2000:
            if view._after:
                view.after_cancel(view._after)
                view._after = None
            view._tick()
            guard += 1
        if view._after:
            view.after_cancel(view._after)
        self.assertIsNone(view.sim)
        records = self.db.list_production()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "simulator")
        self.assertEqual(self.errors(), [])
        # Повторный запуск на ту же смену запрещён (защита от дубликата).
        view.start_sim()
        self.assertTrue(self.errors())
        self.assertIsNone(view.sim)

    def test_simulator_abort_rolls_back(self):
        view = self.app.views["demo"]
        self.app.show_view("demo")
        view.prepare_equipment()
        view.refresh()
        view.sim_date.set_date(date.today() - timedelta(days=2))
        view.start_sim()
        for _ in range(30):
            if view._after:
                view.after_cancel(view._after)
                view._after = None
            view._tick()
        self.assertTrue(self.db.list_events())
        view.abort_sim(ask=False)
        self.assertEqual(self.db.list_events(), [])
        self.assertEqual(self.db.list_downtimes(), [])
        self.assertEqual(self.db.list_production(), [])

    def test_manual_signals(self):
        view = self.app.views["demo"]
        view.prepare_equipment()
        view.refresh()
        view.sig_picker.set_id(view.sig_picker._ids[view.sig_picker.cget("values")[0]])
        view.signal("STOPPED")
        view.signal("STOPPED")
        self.assertIn("пропущен", self.app.status.message.cget("text"))
        view.signal("RUNNING")
        downs = self.db.list_downtimes()
        self.assertEqual(len(downs), 1)
        self.assertIsNotNone(downs[0]["end_at"])

    def test_generator_backup_export_and_cleanup(self):
        view = self.app.views["demo"]
        today = date.today()
        view.gen_from.set_date(today - timedelta(days=5))
        view.gen_to.set_date(today - timedelta(days=1))
        view.generate()
        self.pump()
        self.assertEqual(self.db.count_production(), 15)
        backup_view = self.app.views["backup"]
        backup_view.quick_backup()
        self.pump()
        backup_view.refresh()
        self.assertEqual(len(backup_view.table.rows), 1)
        target = self.tmp / "summary.csv"
        with mock.patch("tkinter.filedialog.asksaveasfilename", return_value=str(target)):
            self.app.views["analytics"].export("summary")
        self.assertTrue(target.exists())
        view.cleanup("demo")
        self.pump()
        self.assertEqual(self.db.count_production(), 0)
        self.assertEqual(self.errors(), [])

    def test_settings_saved(self):
        view = self.app.views["settings"]
        view.threshold.set("75")
        view.save()
        self.assertEqual(Settings.load(self.paths.settings_path).oee_threshold_pct, 75.0)
        view.threshold.set("150")
        view.save()
        self.assertTrue(self.errors())


if __name__ == "__main__":
    unittest.main()
