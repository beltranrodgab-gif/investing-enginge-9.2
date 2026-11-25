"""Point-in-time observation store (SQLite, append-only).

Kept in its own file (``obsidian.sqlite3``) beside the Market Lens journal, which stays the
untouched system of record for the official collector.

Rules:

* **Append-only.** Observations are never updated or deleted (enforced by triggers). A revision
  is a new row with a later ``known_at``.
* **Change-only.** A row is stored only when the value or quality differs from what was already
  known for that period at that time, so re-polling an unchanged release costs nothing.
* **Point in time.** ``as_of(series, at)`` returns, per period, the latest row whose
  ``known_at <= at``. Nothing known later can leak into an earlier query.
* **Fail loudly.** Observations that break the contract are rejected and recorded in ``issue``;
  suspicious ones are stored with ``quality='suspect'`` and an issue, never silently altered.
"""
import json
import sqlite3
from pathlib import Path

from .contract import SeriesMeta, Observation, QUALITY, iso, parse_time, utc_now
from .licenses import license_for
from . import validate

APP_ID = 0x0B51D1A2
FILE_NAME = 'obsidian.sqlite3'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS series(
  series_id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
  category TEXT NOT NULL, unit TEXT NOT NULL, frequency TEXT NOT NULL, license_id TEXT NOT NULL,
  geo TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT '', native_id TEXT NOT NULL DEFAULT '',
  vintages INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, started TEXT NOT NULL, finished TEXT,
  status TEXT NOT NULL DEFAULT 'running', inserted INTEGER NOT NULL DEFAULT 0,
  unchanged INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS obs(
  series_id TEXT NOT NULL REFERENCES series, period TEXT NOT NULL, known_at TEXT NOT NULL,
  vintage TEXT NOT NULL, value REAL, prev_value REAL, published_at TEXT,
  retrieved_at TEXT NOT NULL, quality TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  run_id INTEGER REFERENCES run, extra TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(series_id, period, known_at, vintage)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS obs_pit ON obs(series_id, known_at);
CREATE TABLE IF NOT EXISTS issue(
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES run, at TEXT NOT NULL, series_id TEXT,
  period TEXT, severity TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursor(name TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


def open_store(home_or_path, init=False, read_only=False):
    """Open the store. ``home_or_path`` is a data folder or a ``.sqlite3`` path.

    ``read_only`` opens it with SQLite's ``mode=ro`` (used by the website), so a read path cannot
    write even by mistake.
    """
    p = Path(home_or_path)
    if p.suffix != '.sqlite3':
        p = p / FILE_NAME
    if not init and not p.is_file():
        raise FileNotFoundError('no Obsidian data store yet; run: market_lens.py data init')
    if read_only:
        c = sqlite3.connect('file:%s?mode=ro' % p.resolve().as_posix(), uri=True, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('pragma query_only=on')
        if c.execute('pragma application_id').fetchone()[0] != APP_ID:
            c.close()
            raise ValueError('%s is not an Obsidian data store' % p)
        return c
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    aid = c.execute('pragma application_id').fetchone()[0]
    tables = c.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
    if tables and aid != APP_ID:
        c.close()
        raise ValueError('%s is not an Obsidian data store' % p)
    if init or not tables:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('pragma application_id=%d' % APP_ID)
        c.executescript(SCHEMA)
        for tab in ('obs', 'issue'):
            for action in ('UPDATE', 'DELETE'):
                c.execute("CREATE TRIGGER IF NOT EXISTS forbid_%s_%s BEFORE %s ON %s BEGIN "
                          "SELECT RAISE(ABORT,'append-only: %s rows are never changed'); END"
                          % (tab, action.lower(), action, tab, tab))
        c.commit()
    return c


# --------------------------------------------------------------------------- series

def upsert_series(c, meta: SeriesMeta):
    """Register or refresh a series' metadata. Its license must exist in the register."""
    license_for(meta.license_id)
    problems = validate.validate_meta(meta)
    if problems:
        raise ValueError('%s: %s' % (meta.series_id, '; '.join(problems)))
    c.execute('''INSERT INTO series(series_id,source,name,kind,category,unit,frequency,license_id,
                 geo,currency,native_id,vintages,notes,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(series_id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                 category=excluded.category, unit=excluded.unit, frequency=excluded.frequency,
                 license_id=excluded.license_id, geo=excluded.geo, currency=excluded.currency,
                 native_id=excluded.native_id, vintages=excluded.vintages, notes=excluded.notes,
                 updated=excluded.updated''',
              (meta.series_id, meta.source, meta.name, meta.kind, meta.category, meta.unit,
               meta.frequency, meta.license_id, meta.geo, meta.currency, meta.native_id,
               int(meta.vintages), meta.notes, iso(utc_now())))


def get_series(c, series_id):
    r = c.execute('select * from series where series_id=?', (series_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d['vintages'] = bool(d['vintages'])
    d.pop('updated', None)
    return SeriesMeta(**d)


def list_series(c, source=None, category=None):
    q, args = 'select * from series where 1=1', []
    if source:
        q += ' and source=?'
        args.append(source)
    if category:
        q += ' and category=?'
        args.append(category)
    return [dict(r) for r in c.execute(q + ' order by series_id', args)]


# --------------------------------------------------------------------------- ingestion

def begin_run(c, source):
    cur = c.execute('insert into run(source,started) values(?,?)', (source, iso(utc_now())))
    return cur.lastrowid


def finish_run(c, run_id, summary, status='ok', error=None):
    c.execute('update run set finished=?,status=?,inserted=?,unchanged=?,rejected=?,error=? where id=?',
              (iso(utc_now()), status, summary.get('inserted', 0), summary.get('unchanged', 0),
               summary.get('rejected', 0), (error or '')[:500] or None, run_id))


def _issue(c, run_id, obs, severity, kind, detail):
    c.execute('insert into issue(run_id,at,series_id,period,severity,kind,detail) values(?,?,?,?,?,?,?)',
              (run_id, iso(utc_now()), getattr(obs, 'series_id', None), getattr(obs, 'period', None),
               severity, kind, str(detail)[:500]))


def _known_before(c, series_id, period, known_at):
    return c.execute('select value, quality from obs where series_id=? and period=? and known_at<=? '
                     'order by known_at desc, vintage desc limit 1',
                     (series_id, period, known_at)).fetchone()


def ingest(c, observations, run_id=None, now=None):
    """Validate and store observations. Returns a summary; never raises on bad rows.

    Rows are processed in ``known_at`` order so a batch holding many vintages (an ALFRED history
    load) is stored as the sequence of changes it really was.
    """
    now = now or utc_now()
    summary = {'inserted': 0, 'unchanged': 0, 'revised': 0, 'rejected': 0, 'suspect': 0}
    metas = {}
    batch = sorted((o for o in observations), key=lambda o: (o.series_id, o.known_at or '', o.period))
    by_series = {}
    for o in batch:
        by_series.setdefault(o.series_id, []).append(o)
    for sid, rows in by_series.items():
        meta = metas.get(sid) or get_series(c, sid)
        if meta is None:
            for o in rows:
                _issue(c, run_id, o, 'error', 'unknown_series', 'series is not registered')
            summary['rejected'] += len(rows)
            continue
        metas[sid] = meta
        history = [r['value'] for r in as_of(c, sid, at=iso(now)) if r['value'] is not None]
        flagged = validate.jump_outliers(history, [o.value for o in rows], meta.kind)
        for o, jump in zip(rows, flagged):
            fatal, warnings = validate.validate_observation(o, meta, now)
            if fatal:
                _issue(c, run_id, o, 'error', 'rejected', '; '.join(fatal))
                summary['rejected'] += 1
                continue
            quality = o.quality if o.quality in QUALITY else 'suspect'
            if o.value is None:
                quality = 'missing'
            for w in warnings:
                _issue(c, run_id, o, 'warning', 'validation', w)
                quality = 'suspect'
            if jump:
                _issue(c, run_id, o, 'warning', 'outlier', jump)
                quality = 'suspect'
            prior = _known_before(c, sid, o.period, o.known_at)
            if prior is not None and prior['value'] == o.value and prior['quality'] == quality:
                summary['unchanged'] += 1
                continue
            prev_value = None
            if prior is not None and prior['value'] != o.value:
                prev_value = prior['value']
                # A provisional value (e.g. an intraday last trade) becoming final is a
                # finalisation, not a revision of a published number.
                if quality == 'ok' and prior['quality'] not in ('provisional', 'missing'):
                    quality = 'revised'
                    summary['revised'] += 1
            try:
                c.execute('''insert into obs(series_id,period,known_at,vintage,value,prev_value,published_at,
                             retrieved_at,quality,note,run_id,extra) values(?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (sid, o.period, o.known_at, o.vintage, o.value, prev_value, o.published_at,
                           o.retrieved_at, quality, o.note[:500], run_id,
                           json.dumps(o.extra or {}, sort_keys=True)[:4000]))
            except sqlite3.IntegrityError:
                # Same series/period/known_at/vintage already stored with another value: a
                # source contradicting itself. Record it, keep the first.
                _issue(c, run_id, o, 'error', 'conflict', 'same vintage delivered twice with different values')
                summary['rejected'] += 1
                continue
            summary['inserted'] += 1
            if quality == 'suspect':
                summary['suspect'] += 1
    return summary


# --------------------------------------------------------------------------- point-in-time reads

def as_of(c, series_id, at=None, start=None, end=None):
    """Per period, the latest row known at ``at`` (default now). Oldest period first.

    Missing values are returned as ``value=None`` with their quality, never dropped silently,
    so a caller can tell 'no data' from 'not published yet'.
    """
    at = iso(at) if at else iso(utc_now())
    q = '''select o.* from obs o join (
             select period, max(known_at) k from obs where series_id=? and known_at<=? group by period
           ) last on o.period=last.period and o.known_at=last.k
           where o.series_id=?'''
    args = [series_id, at, series_id]
    if start:
        q += ' and o.period>=?'
        args.append(str(start)[:10])
    if end:
        q += ' and o.period<=?'
        args.append(str(end)[:10])
    rows = {}
    for r in c.execute(q + ' order by o.period, o.vintage', args):
        rows[r['period']] = dict(r)  # several vintages at the same instant: the last id wins
    return [rows[k] for k in sorted(rows)]


def latest(c, series_id, at=None):
    """The newest period known at ``at``, or ``None``."""
    rows = as_of(c, series_id, at)
    return rows[-1] if rows else None


def revisions(c, series_id, period):
    """Every vintage of one period, oldest first."""
    return [dict(r) for r in c.execute(
        'select period, known_at, vintage, value, prev_value, quality from obs '
        'where series_id=? and period=? order by known_at', (series_id, str(period)[:10]))]


def quality_report(c, at=None):
    """Per series: last period, when it was learned, staleness and open issues."""
    at_s = iso(at) if at else iso(utc_now())
    at_t = parse_time(at_s)
    out = []
    for s in c.execute('select * from series order by series_id'):
        last = latest(c, s['series_id'], at_s)
        issues = c.execute("select count(*) from issue where series_id=? and severity='error' and at<=?",
                           (s['series_id'], at_s)).fetchone()[0]
        warns = c.execute("select count(*) from issue where series_id=? and severity='warning' and at<=?",
                          (s['series_id'], at_s)).fetchone()[0]
        status = 'no data'
        if last:
            status = validate.staleness(last['period'], s['frequency'], at_t)
        out.append({'series_id': s['series_id'], 'source': s['source'], 'name': s['name'],
                    'frequency': s['frequency'], 'last_period': last['period'] if last else None,
                    'last_value': last['value'] if last else None,
                    'last_quality': last['quality'] if last else None,
                    'known_at': last['known_at'] if last else None, 'status': status,
                    'errors': issues, 'warnings': warns, 'license_id': s['license_id']})
    return out


def get_cursor(c, name):
    r = c.execute('select value from cursor where name=?', (name,)).fetchone()
    return r['value'] if r else None


def set_cursor(c, name, value):
    c.execute('insert into cursor(name,value) values(?,?) on conflict(name) do update set value=excluded.value',
              (name, str(value)))
