"""Validation rules for series metadata and observations.

Fatal problems reject the row (it never reaches a point-in-time query). Warnings store the row
with ``quality='suspect'`` and an issue record. Nothing is ever corrected or filled in.
"""
import datetime as dt
import math
import statistics

from .contract import FREQUENCIES, KINDS, CATEGORIES, QUALITY, parse_time, _DATE

# How far past the end of a period the next value may reasonably arrive before the series is
# called stale (days). Generous: publication lags differ widely by publisher.
STALE_AFTER_DAYS = {'D': 7, 'W': 21, 'M': 75, 'Q': 200, 'A': 500}

# Clock skew tolerated between a source's timestamps and ours.
SKEW = dt.timedelta(minutes=10)

# A change this many robust standard deviations from the series' typical change is flagged.
JUMP_Z = 10.0


def validate_meta(meta):
    problems = []
    for name in ('series_id', 'source', 'name', 'unit', 'license_id'):
        if not str(getattr(meta, name) or '').strip():
            problems.append('missing ' + name)
    if meta.series_id and not meta.series_id.startswith(meta.source + ':'):
        problems.append("series_id must be prefixed with '%s:'" % meta.source)
    if meta.frequency not in FREQUENCIES:
        problems.append('frequency must be one of %s' % (FREQUENCIES,))
    if meta.kind not in KINDS:
        problems.append('kind must be one of %s' % (KINDS,))
    if meta.category not in CATEGORIES:
        problems.append('category must be one of the research categories')
    if meta.kind in ('price', 'fx') and not meta.currency:
        problems.append('prices need a currency')
    return problems


def validate_observation(obs, meta, now):
    """Return ``(fatal, warnings)`` for one observation against its series metadata."""
    fatal, warnings = [], []
    if not _DATE.match(str(obs.period or '')):
        fatal.append('period must be an ISO date (YYYY-MM-DD), got %r' % obs.period)
    known = parse_time(obs.known_at)
    retrieved = parse_time(obs.retrieved_at)
    if known is None:
        fatal.append('known_at missing or unreadable')
    if retrieved is None:
        fatal.append('retrieved_at missing or unreadable')
    if not str(obs.vintage or '').strip():
        fatal.append('vintage missing')
    if obs.quality not in QUALITY:
        fatal.append('quality must be one of %s' % (QUALITY,))
    if obs.value is not None and (isinstance(obs.value, bool) or not isinstance(obs.value, (int, float))
                                  or not math.isfinite(obs.value)):
        fatal.append('value must be a finite number or None')
    if fatal:
        return fatal, warnings
    # Nothing may claim to be known in the future: that is exactly how look-ahead gets in.
    if known > now + SKEW:
        fatal.append('known_at %s is in the future' % obs.known_at)
    if retrieved > now + SKEW:
        fatal.append('retrieved_at %s is in the future' % obs.retrieved_at)
    # Live data cannot be known before we fetched it. Only sources with genuine historical
    # vintages (ALFRED, completed exchange sessions) may be known before retrieval.
    if not meta.vintages and known < retrieved - SKEW:
        fatal.append('known_at precedes retrieval for a source without historical vintages')
    published = parse_time(obs.published_at) if obs.published_at else None
    if obs.published_at and published is None:
        warnings.append('published_at unreadable: %r' % obs.published_at)
    if published and published > known + SKEW:
        fatal.append('published after it was supposedly known (published_at > known_at)')
    if fatal:
        return fatal, warnings
    period = dt.datetime.fromisoformat(obs.period).replace(tzinfo=dt.timezone.utc)
    # A daily price can be learned on its own day; a month or quarter is dated to its end, and
    # an estimate published before that end is unusual enough to flag.
    if period > known + dt.timedelta(days=1):
        warnings.append('period %s ends after it was known (%s): preliminary or mis-dated' % (obs.period, obs.known_at))
    if meta.kind == 'price' and obs.value is not None and obs.value <= 0:
        warnings.append('non-positive price')
    return fatal, warnings


def jump_outliers(history, new_values, kind):
    """Flag new values whose change is extreme relative to the series' own typical change.

    Uses the median and MAD of past changes (log changes for prices), so one bad print cannot
    hide itself by inflating the spread. Returns one message or ``''`` per new value.
    """
    out = []
    seq = [v for v in history if v is not None]
    use_log = kind in ('price', 'fx', 'commodity')

    def change(a, b):
        if use_log:
            return math.log(b / a) if a and b and a > 0 and b > 0 else None
        return b - a

    for v in new_values:
        msg = ''
        # Hard sanity limit for prices: a tripling or a fall to a third in one observation is
        # almost always a unit, split or decimal error. It is flagged even without history.
        if use_log and v is not None and seq:
            d0 = change(seq[-1], v)
            if d0 is not None and abs(d0) > math.log(3.0):
                msg = 'price moved by a factor of %.2f in one step' % math.exp(d0)
        # Step series (policy rates) have a zero MAD: there is no typical change to compare
        # with, so nothing is flagged rather than every rate decision.
        if not msg and v is not None and len(seq) >= 20:
            diffs = [d for d in (change(a, b) for a, b in zip(seq, seq[1:])) if d is not None]
            d = change(seq[-1], v)
            if d is not None and len(diffs) >= 10:
                med = statistics.median(diffs)
                mad = statistics.median(abs(x - med) for x in diffs) * 1.4826
                if mad > 1e-12:
                    z = (d - med) / mad
                    if abs(z) > JUMP_Z:
                        msg = 'change of %.4g is %.1f robust SDs from typical' % (d, z)
        out.append(msg)
        if v is not None:
            seq.append(v)
    return out


def staleness(last_period, frequency, at):
    """'fresh' or 'stale' given the last period's end date and the frequency."""
    try:
        end = dt.datetime.fromisoformat(str(last_period)[:10]).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return 'unreadable period'
    limit = STALE_AFTER_DAYS.get(frequency, 75)
    return 'stale' if (at - end).days > limit else 'fresh'
