"""Expresiones cron de cinco campos (minuto hora día mes día-de-semana).

Admite ``*``, ``*/n``, rangos ``a-b``, rangos con paso ``a-b/n`` y listas
``a,b,c``. El día de la semana va de 0 a 7 (0 y 7 son domingo). Como en cron,
si se restringen el día del mes y el de la semana, basta con que coincida uno.
Sin dependencias: lo usan la configuración (validación) y el programador.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_FIELDS = (
    ("minuto", 0, 59),
    ("hora", 0, 23),
    ("día", 1, 31),
    ("mes", 1, 12),
    ("día de semana", 0, 7),
)
#: Más allá de esto, una expresión como "0 0 31 2 *" (31 de febrero) no ocurre nunca.
_MAX_DAYS = 366 * 5


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        weekday = (day.weekday() + 1) % 7  # lunes=1 … domingo=0, como cron
        dom = day.day in self.days
        dow = weekday in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow

    def next_after(self, moment: datetime, tz: str = "UTC") -> datetime:
        """Primera fecha y hora (con zona) estrictamente posterior a ``moment``."""
        zone = ZoneInfo(tz)
        local = moment.astimezone(zone).replace(second=0, microsecond=0, tzinfo=None)
        start = local + timedelta(minutes=1)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        day = start.replace(hour=0, minute=0)
        for offset in range(_MAX_DAYS):
            current = day + timedelta(days=offset)
            if not self._day_matches(current):
                continue
            for hour in hours:
                for minute in minutes:
                    candidate = current.replace(hour=hour, minute=minute)
                    if candidate >= start:
                        return candidate.replace(tzinfo=zone)
        raise CronError(f"La expresión {self.expression!r} no ocurre en los próximos 5 años")


def _parse_field(raw: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for item in raw.split(","):
        if not item:
            raise CronError(f"{name}: elemento vacío")
        step, part = 1, item
        if "/" in item:
            part, step_raw = item.split("/", 1)
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise CronError(f"{name}: paso no válido {step_raw!r}")
            step = int(step_raw)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"{name}: rango no válido {part!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = int(part)
            end = high if step > 1 else start
        else:
            raise CronError(f"{name}: valor no válido {part!r}")
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise CronError(f"{name}: {part!r} fuera de {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expression: str) -> Cron:
    parts = expression.split()
    if len(parts) != 5:
        raise CronError("usa cinco campos: minuto hora día mes día-de-semana (p. ej. */15 * * * *)")
    parsed = [
        _parse_field(raw, name, low, high)
        for raw, (name, low, high) in zip(parts, _FIELDS, strict=True)
    ]
    weekdays = frozenset(0 if d == 7 else d for d in parsed[4])
    return Cron(
        expression=expression,
        minutes=parsed[0],
        hours=parsed[1],
        days=parsed[2],
        months=parsed[3],
        weekdays=weekdays,
        any_day=parts[2] == "*",
        any_weekday=parts[4] == "*",
    )
