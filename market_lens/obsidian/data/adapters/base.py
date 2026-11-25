"""Adapter interface. A source plugs in by describing its series and turning bytes into
``Observation`` rows; the store, validation and point-in-time logic are shared.

Parsing is a pure function of the payload so every adapter is tested offline against recorded
payloads. Network access goes through the collector's bounded, TLS-verified ``net.fetch`` and
is restricted to the adapter's declared hosts. Error messages never include query strings, so an
API key cannot leak into logs.
"""
import calendar
import datetime as dt
import os
from urllib.parse import urlsplit

from ..contract import SeriesMeta, Observation, iso, utc_now


class AdapterError(RuntimeError):
    pass


class Adapter:
    source = ''
    license_id = ''
    hosts = ()
    key_env = None          # environment variable holding an API key, if one is needed

    def catalog(self):
        """The series this adapter can provide, as ``SeriesMeta``."""
        raise NotImplementedError

    def fetch(self, series_ids=None, now=None):
        """Fetch observations for ``series_ids`` (default: the whole catalog)."""
        raise NotImplementedError

    # helpers ---------------------------------------------------------------------------
    def key(self):
        if not self.key_env:
            return None
        k = os.environ.get(self.key_env, '').strip()
        if not k:
            raise AdapterError('%s needs an API key in the %s environment variable (free registration '
                               'with the publisher). It is read from the environment only, never '
                               'stored.' % (self.source, self.key_env))
        return k

    def get(self, url):
        host = urlsplit(url).hostname
        if host not in self.hosts:
            raise AdapterError('%s: host %s is not registered for this adapter' % (self.source, host))
        import net  # the collector's bounded HTTPS client
        try:
            body, final_url, _etag, _lmod = net.fetch(url)
        except Exception as e:  # FetchFailure, guard ValueError, URLError ...
            raise AdapterError('%s: request to %s failed (%s)' % (self.source, host, type(e).__name__ + ': ' + _redact(str(e)))) from None
        if urlsplit(final_url).hostname != host:
            raise AdapterError('%s: redirected away from %s' % (self.source, host))
        return body

    def ids(self, series_ids):
        cat = {m.series_id: m for m in self.catalog()}
        if not series_ids:
            return list(cat.values())
        unknown = [s for s in series_ids if s not in cat]
        if unknown:
            raise AdapterError('not in the %s catalog: %s' % (self.source, ', '.join(unknown)))
        return [cat[s] for s in series_ids]


def _redact(text):
    return text.split('?')[0][:200]


def month_end(year, month):
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def period_end_from_start(date_text, frequency):
    """FRED and others date a period by its *first* day; the contract dates it by its last."""
    d = dt.date.fromisoformat(str(date_text)[:10])
    if frequency == 'M':
        return month_end(d.year, d.month).isoformat()
    if frequency == 'Q':
        m = ((d.month - 1) // 3) * 3 + 3
        return month_end(d.year, m).isoformat()
    if frequency == 'A':
        return dt.date(d.year, 12, 31).isoformat()
    return d.isoformat()


def sdmx_period_end(text):
    """SDMX TIME_PERIOD ('2024', '2024-Q1', '2024-03', '2024-W10', '2024-03-12') -> end date."""
    t = str(text).strip()
    try:
        if len(t) == 4 and t.isdigit():
            return dt.date(int(t), 12, 31).isoformat()
        if '-Q' in t:
            y, q = t.split('-Q')
            return month_end(int(y), int(q) * 3).isoformat()
        if '-W' in t:
            y, w = t.split('-W')
            return dt.date.fromisocalendar(int(y), int(w), 7).isoformat()
        if '-S' in t or '-H' in t:
            y, h = t.replace('-H', '-S').split('-S')
            return month_end(int(y), int(h) * 6).isoformat()
        parts = t.split('-')
        if len(parts) == 2:
            return month_end(int(parts[0]), int(parts[1])).isoformat()
        return dt.date.fromisoformat(t[:10]).isoformat()
    except (ValueError, TypeError):
        return None


__all__ = ['Adapter', 'AdapterError', 'SeriesMeta', 'Observation', 'iso', 'utc_now',
           'period_end_from_start', 'sdmx_period_end', 'month_end']
