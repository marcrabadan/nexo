"""time: `now`, `today`, dates, times, durations and time zones (section 15).

A `date` is a day of the calendar. A `time` is an instant, kept in
milliseconds since 1970-01-01 UTC, together with the zone it is shown in:
an IANA name ("Europe/Madrid"), "UTC" or a fixed offset ("+02:00"). Both
print in ISO 8601 (D94). Durations are quantities of the time dimension
(`30s`, `2 days`): a date moves by whole days, a time by milliseconds.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time as _time
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from .. import errors
from ..errors import NexoError
from ..types import Builtin, BuiltinType, Native, Quantity, failure

UTC = dt.timezone.utc
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)
MS_PER_DAY = 86_400_000


class Clock:
    """`now` and `today`: names whose value is read from the clock each
    time they are used (D94)."""

    def __init__(self, name: str):
        self.name = name

    def read(self) -> Any:
        if self.name == "now":
            return Time(int(_time.time() * 1000), local_zone())
        return Time(int(_time.time() * 1000), local_zone()).date()


def local_zone() -> str:
    """The zone of the machine: TZ, then /etc/localtime, then UTC."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz:
        if "zoneinfo/" in tz:
            tz = tz.split("zoneinfo/", 1)[1]
        return tz if valid_zone(tz) else "UTC"
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
            if valid_zone(name):
                return name
    except OSError:
        pass
    return "UTC"


OFFSET_RE = re.compile(r"^([+-])(\d\d):(\d\d)$")


def zone_info(zone: str):
    if zone == "UTC":
        return UTC
    m = OFFSET_RE.match(zone)
    if m:
        minutes = int(m.group(2)) * 60 + int(m.group(3))
        return dt.timezone(dt.timedelta(minutes=minutes if m.group(1) == "+" else -minutes))
    import zoneinfo
    try:
        return zoneinfo.ZoneInfo(zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None


def valid_zone(zone: str) -> bool:
    if not zone or zone.startswith("/") or ".." in zone:
        return False
    return zone_info(zone) is not None


def check_zone(call, zone: Any) -> str:
    from .core import expects
    if not isinstance(zone, str):
        raise expects(call, "zone", "str", zone)
    if not valid_zone(zone):
        raise failure("InvalidValue", call.span, detail=errors.msg("detail.zone", zone=zone))
    return zone


class Date(Native):
    type_name = "date"
    __slots__ = ("year", "month", "day")

    def __init__(self, year: int, month: int, day: int):
        self.year, self.month, self.day = year, month, day

    def key(self):
        return (self.year, self.month, self.day)

    def __eq__(self, other):
        return isinstance(other, Date) and other.key() == self.key()

    def __hash__(self):
        return hash(("date", self.key()))

    def ordinal(self) -> int:
        return dt.date(self.year, self.month, self.day).toordinal()

    @staticmethod
    def from_ordinal(n: int, call=None) -> "Date":
        try:
            d = dt.date.fromordinal(n)
        except (ValueError, OverflowError):
            raise failure("InvalidValue", call.span if call else None, detail=errors.msg("detail.date_range"))
        return Date(d.year, d.month, d.day)

    def repr(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"

    def member(self, call, name: str):
        if name in ("year", "month", "day"):
            return getattr(self, name)
        if name == "weekday":
            return dt.date(self.year, self.month, self.day).isoweekday()
        return self.missing(call, name, ["year", "month", "day", "weekday"])


class Time(Native):
    type_name = "time"
    __slots__ = ("ms", "zone")

    def __init__(self, ms: int, zone: str = "UTC"):
        self.ms, self.zone = ms, zone

    def __eq__(self, other):
        return isinstance(other, Time) and other.ms == self.ms

    def __hash__(self):
        return hash(("time", self.ms))

    def wall(self) -> dt.datetime:
        """The date and time on the clock of its zone."""
        utc = EPOCH + dt.timedelta(milliseconds=self.ms)
        return utc.astimezone(zone_info(self.zone))

    def date(self) -> Date:
        w = self.wall()
        return Date(w.year, w.month, w.day)

    def repr(self) -> str:
        w = self.wall()
        text = f"{w.year:04d}-{w.month:02d}-{w.day:02d}T{w.hour:02d}:{w.minute:02d}:{w.second:02d}"
        ms = self.ms % 1000
        if ms:
            text += f".{ms:03d}"
        offset = int(w.utcoffset().total_seconds()) // 60
        if offset == 0:
            return text + "Z"
        sign = "+" if offset > 0 else "-"
        offset = abs(offset)
        return f"{text}{sign}{offset // 60:02d}:{offset % 60:02d}"

    def member(self, call, name: str):
        names = ["year", "month", "day", "hour", "minute", "second", "date", "zone", "to_zone"]
        if name in ("year", "month", "day", "hour", "minute", "second"):
            return getattr(self.wall(), name)
        if name == "date":
            return self.date()
        if name == "zone":
            return self.zone
        if name == "to_zone":
            return Builtin("to_zone", lambda c, zone: Time(self.ms, check_zone(c, zone)))
        return self.missing(call, name, names)


def wall_to_ms(year, month, day, hour, minute, second, ms, zone) -> int:
    """The instant a wall clock shows in a zone; a time skipped or
    repeated by a change of offset takes the offset in effect before it
    (PEP 495, fold 0)."""
    local = dt.datetime(year, month, day, hour, minute, second, ms * 1000, tzinfo=zone_info(zone))
    delta = local - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def b_date(call, year, month=None, day=None):
    from .core import expects
    if isinstance(year, Time) and month is None and day is None:
        return year.date()
    for v in (year, month, day):
        if not isinstance(v, int) or isinstance(v, bool):
            raise expects(call, "date", "int", v)
    try:
        dt.date(year, month, day)
    except ValueError:
        raise failure("InvalidValue", call.span, detail=errors.msg("detail.bad_date",
                      value=f"{year}-{month:02d}-{day:02d}"))
    return Date(year, month, day)


def b_time(call, year, month, day, hour=0, minute=0, second=0, zone=None):
    from .core import expects
    for v in (year, month, day, hour, minute, second):
        if not isinstance(v, int) or isinstance(v, bool):
            raise expects(call, "time", "int", v)
    zone = local_zone() if zone is None else check_zone(call, zone)
    try:
        ms = wall_to_ms(year, month, day, hour, minute, second, 0, zone)
    except (ValueError, OverflowError):
        raise failure("InvalidValue", call.span, detail=errors.msg("detail.bad_date",
                      value=f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"))
    return Time(ms, zone)


DATE_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)$")
TIME_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,3}))?)?(Z|[+-]\d\d:\d\d)$")


def parse_date(text: str) -> Date | None:
    m = DATE_RE.match(text)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        dt.date(y, mo, d)
    except ValueError:
        return None
    return Date(y, mo, d)


def parse_time(text: str) -> Time | None:
    m = TIME_RE.match(text)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups()[:5])
    s = int(m.group(6) or 0)
    frac = m.group(7) or ""
    ms = int((frac + "00")[:3]) if frac else 0
    zone = "UTC" if m.group(8) == "Z" else m.group(8)
    if zone in ("+00:00", "-00:00"):
        zone = "UTC"
    try:
        return Time(wall_to_ms(y, mo, d, h, mi, s, ms, zone), zone)
    except ValueError:
        return None


# ---------------------------------------------------------- arithmetic

def duration_ms(q: Quantity, span) -> int:
    return int((q.base() * 1000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def date_time_op(op: str, a: Any, b: Any, span) -> Any:
    """date ± days, date - date, time ± duration, time - time."""
    if isinstance(a, Date) and isinstance(b, Quantity) and b.dimension == "time" and op in ("+", "-"):
        seconds = b.base()
        if seconds % 86400 != 0:
            raise NexoError("date_days", span, value=errors_repr(b))
        days = int(seconds / 86400)
        return Date.from_ordinal(a.ordinal() + (days if op == "+" else -days), _Span(span))
    if isinstance(a, Quantity) and isinstance(b, Date) and op == "+":
        return date_time_op("+", b, a, span)
    if isinstance(a, Date) and isinstance(b, Date) and op == "-":
        return Quantity(Decimal(a.ordinal() - b.ordinal()), "days")
    if isinstance(a, Time) and isinstance(b, Quantity) and b.dimension == "time" and op in ("+", "-"):
        delta = duration_ms(b, span)
        return Time(a.ms + delta if op == "+" else a.ms - delta, a.zone)
    if isinstance(a, Quantity) and isinstance(b, Time) and op == "+":
        return date_time_op("+", b, a, span)
    if isinstance(a, Time) and isinstance(b, Time) and op == "-":
        from ..numbers import normalize
        return Quantity(normalize(Decimal(a.ms - b.ms) / 1000), "s")
    raise NexoError("bad_operands", span, op=op, a=type_label(a), b=type_label(b))


class _Span:
    def __init__(self, span):
        self.span = span


def errors_repr(v) -> str:
    from ..types import repr_value
    return repr_value(v)


def type_label(v) -> str:
    from ..types import type_name
    return type_name(v)


def exports() -> dict:
    return {
        "now": Clock("now"),
        "today": Clock("today"),
        "date": BuiltinType("date"),
        "time": BuiltinType("time"),
        "__convert_date": Builtin("date", b_date),
        "__convert_time": Builtin("time", b_time),
    }
