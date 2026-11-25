"""Bridge: the Market Lens journal (official publishers) into the Obsidian point-in-time store.

The journal stays the untouched system of record. This reads it (never writes it) and turns
every accepted snapshot into a vintage: ``known_at`` is the time the collector accepted the
snapshot (``ready.indexed``), ``retrieved_at`` is when the bytes were received (``got.seen``),
and ``vintage`` is the snapshot's content hash. Unchanged snapshots cost nothing (change-only
storage). A cursor makes repeated syncs incremental.
"""
import sources as ml_sources
from reliability import quality as ml_quality
from signal_filter import period_end

from .base import SeriesMeta, Observation
from ..contract import clean_value, iso, parse_time
from .. import store as obs_store

CURSOR = 'marketlens.ready_id'

CATEGORY_BY_FACTOR = {'ACT': 'growth', 'LAB': 'employment', 'INF': 'inflation', 'YLD': 'rates',
                      'FX-': 'fx', 'COM': 'commodities', 'IDX': 'markets', 'MP-': 'central_bank',
                      'MON': 'liquidity', 'SUP': 'trade'}
QUALITY_MAP = {'unflagged / normal': 'ok', 'provisional': 'provisional', 'estimated': 'estimated',
               'missing': 'missing'}


def _kind_and_currency(m):
    f = (m.get('factor') or '')[:3]
    unit = m.get('unit') or ''
    if f == 'FX-' and ' per ' in unit:
        return 'fx', unit.split(' per ')[0].strip()[:3].upper()
    if f in ('YLD', 'MP-'):
        return 'rate', ''
    if f == 'COM':
        return 'commodity', ''
    return 'macro', ''


def series_meta(sid):
    m = ml_sources.M[sid]
    kind, cur = _kind_and_currency(m)
    if kind == 'fx' and not cur:
        kind = 'macro'
    return SeriesMeta(series_id='marketlens:' + sid, source='marketlens', name=m.get('label') or sid,
                      kind=kind, category=CATEGORY_BY_FACTOR.get((m.get('factor') or '')[:3], 'markets'),
                      unit=m.get('unit') or 'unspecified', frequency=m.get('freq') or 'M',
                      license_id='marketlens-official', geo=(m.get('geos') or [''])[0],
                      currency=cur, native_id=sid, vintages=False,
                      notes=(m.get('relation') or '')[:200])


def catalog():
    return [series_meta(sid) for sid in sorted(ml_sources.M)]


def sync(journal, obs_conn, limit=None, now=None):
    """Import accepted journal snapshots newer than the cursor. Returns a summary.

    ``journal`` is an open Market Lens connection (read only is enough).
    """
    for meta in catalog():
        obs_store.upsert_series(obs_conn, meta)
    start = int(obs_store.get_cursor(obs_conn, CURSOR) or 0)
    q = ('select r.id, r.indexed, r.parser, g.seen, g.h, p.feed from ready r join got g using(id) '
         'join poll p using(id) where r.id>? order by r.id')
    rows = journal.execute(q + (' limit %d' % int(limit) if limit else ''), (start,)).fetchall()
    run = obs_store.begin_run(obs_conn, 'marketlens')
    total = {'snapshots': 0, 'inserted': 0, 'unchanged': 0, 'revised': 0, 'rejected': 0, 'suspect': 0}
    last_hash = {}
    last_id = start
    try:
        for r in rows:
            last_id = r['id']
            key = (r['feed'], r['parser'])
            if last_hash.get(key) == r['h']:
                continue  # identical bytes to the previous accepted check of this feed
            last_hash[key] = r['h']
            known = iso(r['indexed'])
            seen = iso(r['seen'])
            if not known or not seen:
                continue
            batch = []
            for s in journal.execute('select series,t,x,flag,pub from snap where feed=? and h=? and parser=?',
                                     (r['feed'], r['h'], r['parser'])):
                m = ml_sources.M.get(s['series'])
                if m is None:
                    continue
                period = period_end(s['t'])
                if not period:
                    continue
                value = clean_value(s['x'])
                q = ml_quality(m, {'value': s['x'], 'flag': s['flag'] or ''})
                quality = QUALITY_MAP.get(q, 'suspect')
                pub = parse_time(s['pub']) if s['pub'] else None
                published = iso(pub) if pub and iso(pub) <= known else None
                batch.append(Observation(series_id='marketlens:' + s['series'], period=period, value=value,
                                         known_at=known, retrieved_at=min(seen, known), vintage=r['h'][:16],
                                         published_at=published, quality=quality,
                                         note='' if quality != 'suspect' else q[:120]))
            res = obs_store.ingest(obs_conn, batch, run, now=now)
            total['snapshots'] += 1
            for k in ('inserted', 'unchanged', 'revised', 'rejected', 'suspect'):
                total[k] += res[k]
        obs_store.set_cursor(obs_conn, CURSOR, last_id)
        obs_store.finish_run(obs_conn, run, total)
        obs_conn.commit()
    except Exception as e:
        obs_conn.rollback()
        run = obs_store.begin_run(obs_conn, 'marketlens')
        obs_store.finish_run(obs_conn, run, total, status='failed', error=str(e))
        obs_conn.commit()
        raise
    total['cursor'] = last_id
    return total
