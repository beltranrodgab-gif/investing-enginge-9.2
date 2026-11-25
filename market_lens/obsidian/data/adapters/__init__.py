"""Registered network adapters. The Market Lens bridge is separate: it reads the local journal."""
from .base import Adapter, AdapterError
from .fred import FredAdapter
from .ecb import EcbAdapter
from .yahoo import YahooAdapter

ADAPTERS = {'fred': FredAdapter, 'ecb': EcbAdapter, 'yahoo': YahooAdapter}


def get_adapter(name, **kw):
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise AdapterError('unknown source %r; known: %s' % (name, ', '.join(sorted(ADAPTERS)))) from None
    return cls(**kw)
