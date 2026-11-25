"""The observation contract every data source must satisfy.

An observation is one value of one series for one period, *as known at one moment*. The
fields separate the four times that look-ahead bias confuses:

* ``period``       — what the value describes (the reference date, end of period).
* ``published_at`` — when the publisher released it, if known.
* ``retrieved_at`` — when this system obtained it.
* ``known_at``     — the earliest time a point-in-time query may see it. For live collection
  it is ``retrieved_at``. For sources with true historical vintages (ALFRED) it is the public
  release time, conservatively placed at the end of the release day.

A missing value is ``value=None`` with ``quality='missing'``. It is never filled in.
"""
import datetime as dt
import math
import re
from dataclasses import dataclass, field, asdict

FREQUENCIES = ('D', 'W', 'M', 'Q', 'A')
KINDS = ('macro', 'rate', 'price', 'fx', 'commodity', 'flow', 'fiscal', 'other')
# Ordered from best to worst. ``revised`` marks a value that replaced an earlier vintage.
QUALITY = ('ok', 'provisional', 'estimated', 'revised', 'stale', 'suspect', 'missing')

# Research categories used by the macro modules (prompt Part 4).
CATEGORIES = ('growth', 'inflation', 'employment', 'central_bank', 'rates', 'liquidity',
              'fiscal', 'debt', 'trade', 'fx', 'commodities', 'credit', 'financial_conditions',
              'geopolitics', 'capital_flows', 'markets', 'housing', 'consumer')

_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    """Canonical UTC timestamp string ``YYYY-MM-DDTHH:MM:SSZ``."""
    if t is None:
        return None
    if isinstance(t, str):
        return parse_time(t) and iso(parse_time(t))
    if isinstance(t, dt.date) and not isinstance(t, dt.datetime):
        t = dt.datetime(t.year, t.month, t.day, tzinfo=dt.timezone.utc)
    if t.tzinfo is None:
        raise ValueError('naive datetime: timestamps must be timezone-aware UTC')
    return t.astimezone(dt.timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(s):
    """Parse an ISO timestamp or date into an aware UTC datetime; ``None`` if unreadable."""
    if s is None:
        return None
    if isinstance(s, dt.datetime):
        return s.astimezone(dt.timezone.utc) if s.tzinfo else None
    text = str(s).strip()
    if _DATE.match(text):
        d = dt.date.fromisoformat(text)
        return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
    try:
        t = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t.astimezone(dt.timezone.utc) if t.tzinfo else None


def end_of_day(date_text):
    """``2024-03-12`` -> ``2024-03-12T23:59:59Z``: a conservative 'known by' time for a release day."""
    d = dt.date.fromisoformat(str(date_text)[:10])
    return iso(dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=dt.timezone.utc))


@dataclass(frozen=True)
class SeriesMeta:
    series_id: str              # canonical, source-prefixed: 'fred:CPIAUCSL'
    source: str                 # adapter id: 'fred', 'ecb', 'yahoo', 'marketlens'
    name: str
    kind: str                   # one of KINDS
    category: str               # one of CATEGORIES
    unit: str
    frequency: str              # one of FREQUENCIES
    license_id: str
    geo: str = ''               # ISO country / 'EA' / 'WORLD'
    currency: str = ''          # for prices and money amounts
    native_id: str = ''         # the id the publisher uses
    vintages: bool = False      # True when the source provides genuine historical vintages
    notes: str = ''

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Observation:
    series_id: str
    period: str                 # ISO date, end of the reference period
    value: float = None         # None = missing; never fabricated
    known_at: str = ''
    retrieved_at: str = ''
    vintage: str = ''
    published_at: str = None
    quality: str = 'ok'
    note: str = ''
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def clean_value(x):
    """A finite float or ``None``. Publisher placeholders ('.', 'NA', '') become ``None``."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, str) and x.strip() in ('', '.', 'NA', 'N/A', 'NaN', 'nan', '-', '--'):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None
