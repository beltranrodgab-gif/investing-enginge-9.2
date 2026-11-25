"""High-level data operations shared by the CLI (``market_lens.py data ...``) and the website."""
import contextlib

from . import store
from .licenses import register
from .adapters import get_adapter, ADAPTERS
from .adapters import marketlens as bridge


def init(home):
    with contextlib.closing(store.open_store(home, init=True)) as c:
        for name in sorted(ADAPTERS):
            for meta in get_adapter(name).catalog():
                store.upsert_series(c, meta)
        for meta in bridge.catalog():
            store.upsert_series(c, meta)
        c.commit()
        return {'store': str(home), 'series': len(store.list_series(c))}


def sync_journal(home, journal, now=None):
    """Import the Market Lens journal into the point-in-time store (incremental)."""
    with contextlib.closing(store.open_store(home, init=True)) as c:
        return bridge.sync(journal, c, now=now)


def fetch(home, source, series_ids=None, symbols=None, now=None):
    """Fetch from one network source into the store. Errors are recorded on the run and re-raised."""
    kw = {'symbols': symbols} if source == 'yahoo' and symbols else {}
    adapter = get_adapter(source, **kw)
    with contextlib.closing(store.open_store(home, init=True)) as c:
        for meta in adapter.catalog():
            store.upsert_series(c, meta)
        c.commit()
        run = store.begin_run(c, source)
        try:
            rows = adapter.fetch(series_ids or None, now=now)
            summary = store.ingest(c, rows, run, now=now)
            store.finish_run(c, run, summary)
            c.commit()
        except Exception as e:
            c.rollback()
            run = store.begin_run(c, source)
            store.finish_run(c, run, {}, status='failed', error=str(e))
            c.commit()
            raise
        summary['source'] = source
        summary['observations'] = len(rows)
        return summary


def as_of(home, series_id, at=None, start=None, end=None):
    with contextlib.closing(store.open_store(home, read_only=True)) as c:
        meta = store.get_series(c, series_id)
        if meta is None:
            raise ValueError('unknown series: %s' % series_id)
        return {'series': meta.to_dict(), 'as_of': at, 'observations': store.as_of(c, series_id, at, start, end)}


def quality(home, at=None):
    with contextlib.closing(store.open_store(home, read_only=True)) as c:
        return store.quality_report(c, at)


def series(home, source=None, category=None):
    with contextlib.closing(store.open_store(home, read_only=True)) as c:
        return store.list_series(c, source, category)


def runs(home, limit=20):
    with contextlib.closing(store.open_store(home, read_only=True)) as c:
        return [dict(r) for r in c.execute('select * from run order by id desc limit ?', (int(limit),))]


def sources():
    return {'licenses': register(),
            'adapters': {name: {'license': cls.license_id, 'hosts': list(cls.hosts),
                                'key_env': cls.key_env} for name, cls in sorted(ADAPTERS.items())},
            'bridge': {'marketlens': {'license': 'marketlens-official', 'reads': 'local journal'}}}


def web_view(home, path, arg):
    """Read-only JSON for the website: /api/data/{sources,series,quality,asof,runs}."""
    name = path.rsplit('/', 1)[-1]
    if name == 'sources':
        return sources()
    if name == 'series':
        return series(home, arg('source') or None, arg('category') or None)
    if name == 'quality':
        return quality(home, arg('at') or None)
    if name == 'runs':
        return runs(home)
    if name == 'asof':
        sid = arg('id')
        if not sid:
            raise ValueError('id is required')
        return as_of(home, sid, arg('at') or None, arg('start') or None, arg('end') or None)
    raise KeyError(path)
