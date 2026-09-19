"""Pure temporal model; archive time is UTC, calendar boundaries are local."""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo
import math

UTC = timezone.utc
RATES = (.5, 1., 2., 4.)


class PlaybackError(Exception):
    """Only fixed, public error codes cross into the UI or diagnostics."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Cancelled(PlaybackError):
    def __init__(self):
        super().__init__('cancelled')


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled()


def local_candidates(value, zone):
    """Reject spring gaps; return BOTH UTC instants for an autumn repeated hour."""
    if value.tzinfo is not None:
        return (value.astimezone(UTC),)
    zone = ZoneInfo(zone) if isinstance(zone, str) else zone
    result = set()
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == value:
            result.add(candidate)
    return tuple(sorted(result))


def parse_time(raw, zone=None, shift=0):
    try:
        value = datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
        if value.tzinfo is None:
            if not zone:
                raise PlaybackError('timezone-required')
            candidates = local_candidates(value, zone)
            if len(candidates) != 1:
                raise PlaybackError('ambiguous-time' if candidates else 'nonexistent-time')
            value = candidates[0]
        return value.astimezone(UTC).timestamp() + shift
    except (ValueError, OverflowError):
        raise PlaybackError('invalid-time') from None


def day_bounds(day, zone):
    tz = ZoneInfo(zone) if isinstance(zone, str) else zone
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC)
    return start.timestamp(), end.timestamp()


def covered_days(start, end, zone):
    tz = ZoneInfo(zone) if isinstance(zone, str) else zone
    first = datetime.fromtimestamp(start, tz).date()
    # Half-open intervals: an end at midnight does not mark the following day.
    last = datetime.fromtimestamp(max(start, (end or start) - .000001), tz).date()
    if (last - first).days > 366:
        raise PlaybackError('invalid-interval')
    return tuple(first + timedelta(days=n) for n in range((last-first).days + 1))


def iso_utc(stamp):
    return datetime.fromtimestamp(stamp, UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


@dataclass(frozen=True)
class Camera:
    camera_id: int
    host: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    backend: str = 'auto'
    zone: str = ''
    endpoint: str = field(default='', repr=False)
    track: str = ''
    revision: str = ''
    time_shift: int = 0


@dataclass(frozen=True)
class Recording:
    camera_id: int
    device: str
    backend: str
    track: str
    native_id: str = field(repr=False)
    start: float
    end: float = None
    size: int = 0
    locator: str = field(default='', repr=False)
    raw_start: str = ''
    raw_end: str = ''
    observed: float = 0.
    finalization: str = 'unconfirmed'

    def __post_init__(self):
        if not math.isfinite(self.start) or (self.end is not None and
                (not math.isfinite(self.end) or not self.start < self.end <= self.start + 366*86400)):
            raise PlaybackError('invalid-interval')
        if self.size < 0:
            raise PlaybackError('invalid-size')

    @property
    def identity(self):
        return sha256(f'{self.camera_id}|{self.device}|{self.backend}|{self.track}|{self.native_id}'.encode()).hexdigest()

    @property
    def key(self):
        return sha256(f'{self.identity}|{self.raw_start}|{self.raw_end}|{self.size}'.encode()).hexdigest()

    def contains(self, stamp):
        return self.end is not None and self.start <= stamp < self.end


@dataclass(frozen=True)
class SearchResult:
    records: tuple = ()
    complete: bool = False
    reason: str = ''
    observed: float = 0.


@dataclass(frozen=True)
class Controls:
    paused: bool = False
    rate: float = 1.
    muted: bool = True
    volume: int = 70


@dataclass(frozen=True)
class Viewport:
    start: float
    span: float = 1800.

    def at(self, x, width):
        return self.start + max(0., min(1., x / max(1., width))) * self.span

    def zoom(self, factor, anchor):
        span = max(120., min(90000., self.span * factor))
        relative = max(0., min(1., (anchor-self.start) / self.span))
        return Viewport(anchor-relative*span, span)


def layout_mode(width, height, scale=1., previous='wide'):
    width, height = width/max(.5, scale), height/max(.5, scale)
    if height < (510 if previous == 'short' else 470):
        return 'short'
    if width < (820 if previous == 'compact' else 760):
        return 'compact'
    return 'wide' if width >= (1080 if previous != 'wide' else 1020) else 'medium'
