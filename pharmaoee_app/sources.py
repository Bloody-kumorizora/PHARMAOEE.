"""Источники данных о состоянии оборудования и обработка их событий.

Сейчас реализованы только демонстрационные источники: ручные сигналы-кнопки
и симулятор смены. Подключения к реальным PLC/SCADA/MES/OPC UA нет.
Будущий источник должен реализовать ``DataSource.poll()`` и возвращать
``TelemetryEvent``; события проверяются ``validate_event`` и применяются
``EventProcessor`` — тем же путём, что и события симулятора.

Правила обработки состояний:

* RUNNING — оборудование работает; закрывает открытый простой.
* STOPPED — остановка; открывает простой с причиной «Не классифицировано»
  (причину указывает оператор, программа её не угадывает).
* CONNECTION_LOST — нет данных от источника. Это НЕ простой: состояние
  оборудования неизвестно. Если до потери связи шёл простой, он остаётся
  открытым и закрывается следующим сигналом RUNNING с пометкой в комментарии.
* Повтор текущего состояния игнорируется (защита от двойного клика).
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import SOURCE_SIMULATOR, UNCLASSIFIED_REASON
from .db import Database, parse_ts
from .validation import DowntimeInput, ProductionInput, shift_bounds, shift_for

RUNNING, STOPPED, CONNECTION_LOST = "RUNNING", "STOPPED", "CONNECTION_LOST"
STATES = (RUNNING, STOPPED, CONNECTION_LOST)
STATE_LABELS = {RUNNING: "Работает", STOPPED: "Остановлено", CONNECTION_LOST: "Нет связи"}
MAX_FUTURE_SKEW = timedelta(minutes=5)
LOST_NOTE = "В интервале была потеря связи — проверьте фактическое время окончания."


@dataclass(frozen=True)
class TelemetryEvent:
    """Событие источника: что произошло, где, когда и когда получено."""

    equipment_id: int
    state: str
    timestamp: datetime          # время события по часам источника
    source_name: str
    received_at: datetime        # время получения приложением
    simulated: bool = True       # True — данные сгенерированы программой
    error: str | None = None     # ошибка связи или данных, сообщённая источником


class DataSource(ABC):
    """Интерфейс источника телеметрии для будущих интеграций."""

    name: str = "Источник"
    simulated: bool = True

    @abstractmethod
    def poll(self) -> list[TelemetryEvent]:
        """Новые события с момента прошлого вызова (может вернуть пустой список)."""


def validate_event(event: TelemetryEvent, current_start: datetime | None, now: datetime) -> list[str]:
    errors = []
    if event.state not in STATES:
        errors.append(f"Неизвестное состояние «{event.state}».")
    if not isinstance(event.equipment_id, int) or event.equipment_id <= 0:
        errors.append("Не указан идентификатор оборудования.")
    if not event.simulated and event.timestamp > now + MAX_FUTURE_SKEW:
        errors.append("Время события в будущем — проверьте часы источника.")
    if current_start is not None and event.timestamp < current_start:
        errors.append("Событие старше текущего состояния: нарушен порядок событий.")
    if event.error and event.state != CONNECTION_LOST:
        errors.append(f"Источник сообщил об ошибке: {event.error}")
    return errors


@dataclass(frozen=True)
class ApplyResult:
    status: str                  # applied / ignored / rejected
    message: str
    event_id: int | None = None
    downtime_id: int | None = None          # открытый этим событием простой
    closed_downtime_id: int | None = None


class EventProcessor:
    """Применяет события источника к базе в одной транзакции на событие."""

    def __init__(self, db: Database):
        self.db = db

    def apply(self, event: TelemetryEvent, now: datetime | None = None) -> ApplyResult:
        now = now or datetime.now()
        db = self.db
        with db.transaction() as con:
            if con.execute("SELECT 1 FROM equipment WHERE id = ?", (event.equipment_id,)).fetchone() is None:
                return ApplyResult("rejected", "Оборудование не найдено.")
            current = db.get_open_event(con, event.equipment_id)
            if current and current["state"] == event.state:
                return ApplyResult("ignored", f"Состояние «{STATE_LABELS[event.state]}» уже зафиксировано — "
                                              "повторный сигнал пропущен.")
            errors = validate_event(event, parse_ts(current["start_at"]) if current else None, now)
            if errors:
                return ApplyResult("rejected", " ".join(errors))

            carried = None          # открытый простой, тянущийся из прошлого состояния
            passed_lost = False
            if current:
                db.close_event(con, current["id"], event.timestamp)
                if current["state"] == STOPPED and not current["downtime_id"]:
                    # Событие остановки из v8 без простоя — создаём простой с его начала.
                    carried = self._open_downtime(con, event.equipment_id, parse_ts(current["start_at"]),
                                                  current["source_name"])
                elif current["state"] in (STOPPED, CONNECTION_LOST) and db.is_downtime_open(con, current["downtime_id"]):
                    carried = current["downtime_id"]
                    passed_lost = current["state"] == CONNECTION_LOST

            closed = opened = None
            comment = ""
            if event.state == RUNNING:
                if carried and db.end_open_downtime(con, carried, event.timestamp, LOST_NOTE if passed_lost else ""):
                    closed = carried
                link = None
            elif event.state == STOPPED:
                if carried:
                    link = carried
                else:
                    conflicts = db.find_overlapping_downtimes(con, event.equipment_id, event.timestamp, None)
                    if conflicts:
                        link = None
                        comment = (f"Простой не создан: время уже учтено в простое ID {conflicts[0]['id']} "
                                   "(без двойного учёта).")
                    else:
                        link = opened = self._open_downtime(con, event.equipment_id, event.timestamp,
                                                            event.source_name)
            else:
                link = carried
                comment = "Нет данных от источника; состояние оборудования неизвестно (не считается простоем)."
                if event.error:
                    comment += f" Ошибка источника: {event.error}"
            event_id = db.insert_event(con, event.equipment_id, event.state, event.timestamp,
                                       source_name=event.source_name, comment=comment, downtime_id=link,
                                       received_at=event.received_at)
        return ApplyResult("applied", f"Зафиксировано: {STATE_LABELS[event.state]}.", event_id, opened, closed)

    def _open_downtime(self, con, equipment_id: int, start: datetime, source_name: str) -> int:
        work_date, shift = shift_for(start)
        data = DowntimeInput(equipment_id, work_date, shift, start, None, UNCLASSIFIED_REASON,
                             f"Создано по сигналу остановки ({source_name}). Причину указывает оператор.")
        return self.db.insert_downtime(con, data, SOURCE_SIMULATOR)

    def close_session(self, equipment_id: int, at: datetime) -> int | None:
        """Завершает текущее состояние без нового (конец сеанса симулятора)."""
        with self.db.transaction() as con:
            current = self.db.get_open_event(con, equipment_id)
            if not current:
                return None
            end = max(at, parse_ts(current["start_at"]))
            self.db.close_event(con, current["id"], end)
            if current["downtime_id"] and self.db.end_open_downtime(con, current["downtime_id"], end):
                return current["downtime_id"]
        return None


# --- Симулятор смены ------------------------------------------------------------------

@dataclass
class SimCounters:
    minute: int = 0
    run_min: int = 0
    stop_min: int = 0
    lost_min: int = 0
    total: int = 0
    good: int = 0
    reported_total: int = 0      # что «видит» приложение: во время потери связи не растёт
    reported_good: int = 0
    stops: int = 0


@dataclass
class ShiftSimulator(DataSource):
    """Детерминированная модель одной смены. Один шаг = 1 минута смены.

    Время работы во время потери связи считается работой (оборудование не
    останавливалось), а счётчики выпуска передаются после восстановления связи.
    """

    equipment_id: int
    equipment_name: str
    work_date: date
    shift: str
    ideal_cycle_sec: float
    seed: int = 2026
    stop_probability: float = 0.02
    lost_probability: float = 0.004
    counters: SimCounters = field(default_factory=SimCounters)
    name: str = "Симулятор смены (демо)"
    simulated: bool = True

    def __post_init__(self):
        self.rng = random.Random(f"sim|{self.seed}|{self.equipment_name}|{self.work_date}|{self.shift}")
        self.start, self.end = shift_bounds(self.work_date, self.shift)
        self.length = int((self.end - self.start).total_seconds() // 60)
        self.state = RUNNING             # фактическое состояние модели
        self.reported = None             # последнее сообщённое состояние
        self.remaining = 0               # минут до окончания остановки
        self.lost_remaining = 0
        self.speed = self.rng.uniform(0.86, 0.96)
        self._carry = 0.0
        self.timeline: list[tuple[int, str]] = []    # (минута, сообщённое состояние)

    @property
    def finished(self) -> bool:
        return self.counters.minute >= self.length

    @property
    def clock(self) -> datetime:
        return self.start + timedelta(minutes=self.counters.minute)

    def _emit(self, state: str, events: list[TelemetryEvent]) -> None:
        if state != self.reported:
            self.reported = state
            self.timeline.append((self.counters.minute, state))
            events.append(TelemetryEvent(self.equipment_id, state, self.clock, self.name, datetime.now()))

    def poll(self) -> list[TelemetryEvent]:
        return self.step()

    def step(self) -> list[TelemetryEvent]:
        if self.finished:
            return []
        events: list[TelemetryEvent] = []
        c = self.counters
        left = self.length - c.minute
        if c.minute == 0:
            self._emit(RUNNING, events)
        if self.lost_remaining == 0 and self.reported == CONNECTION_LOST:
            self._emit(self.state, events)
        if self.state == STOPPED and self.remaining == 0:
            self.state = RUNNING
            if self.reported != CONNECTION_LOST:
                self._emit(RUNNING, events)
        elif self.state == RUNNING and self.reported != CONNECTION_LOST and left > 3:
            roll = self.rng.random()
            if roll < self.stop_probability:
                self.state = STOPPED
                self.remaining = min(self.rng.randint(3, 25), left)
                c.stops += 1
                self._emit(STOPPED, events)
            elif roll < self.stop_probability + self.lost_probability:
                self.lost_remaining = min(self.rng.randint(2, 6), left - 1)
                self._emit(CONNECTION_LOST, events)

        if self.state == RUNNING:
            c.run_min += 1
            expected = 60.0 / self.ideal_cycle_sec * self.speed * self.rng.uniform(0.97, 1.03) + self._carry
            made = int(expected)
            self._carry = expected - made
            defects = sum(1 for _ in range(min(made, 400)) if self.rng.random() < 0.008)
            c.total += made
            c.good += made - defects
        else:
            c.stop_min += 1
            self.remaining -= 1
        if self.reported == CONNECTION_LOST:
            c.lost_min += 1
            self.lost_remaining = max(0, self.lost_remaining - 1)
        else:
            c.reported_total, c.reported_good = c.total, c.good
        c.minute += 1
        if self.finished:
            c.reported_total, c.reported_good = c.total, c.good
            if self.state == STOPPED or self.reported != RUNNING:
                self.state = RUNNING
                self._emit(RUNNING, events)
        return events

    def production_record(self) -> ProductionInput:
        c = self.counters
        return ProductionInput(self.equipment_id, self.work_date, self.shift, float(self.length),
                               float(c.run_min), c.total, c.good, self.ideal_cycle_sec,
                               "Сеанс симулятора смены (демонстрационные данные)")
