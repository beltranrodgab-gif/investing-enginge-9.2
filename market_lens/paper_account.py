"""Durable paper-trading account state.

The account lives in its own JSON file next to the journal rather than in SQLite, because the
dashboard opens the journal ``query_only`` and the paper account must never be able to write to
the collected record. Every write is atomic (temp file + ``os.replace``) and every read is
validated, so a truncated file degrades to ``None`` and the caller starts a fresh account instead
of trading on corrupted state.
"""
import json, os, tempfile
from pathlib import Path

DEFAULT_NAME = 'paper_account.json'


def default_path(home):
    return Path(home) / DEFAULT_NAME


def _validate(state):
    if not isinstance(state, dict):
        return None
    nav = state.get('nav')
    if isinstance(nav, bool) or not isinstance(nav, (int, float)) or nav <= 0:
        return None
    weights = state.get('weights')
    if not isinstance(weights, dict):
        return None
    clean = {}
    for k, v in weights.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return None
        clean[str(k)] = float(v)
    if sum(clean.values()) > 1.0 + 1e-9:
        return None
    out = {'nav': float(nav), 'weights': clean,
           'cash': float(state.get('cash', max(0.0, 1.0 - sum(clean.values())))),
           'periods': int(state.get('periods', 0) or 0)}
    initial = state.get('initial_nav')
    if isinstance(initial, (int, float)) and not isinstance(initial, bool) and initial > 0:
        out['initial_nav'] = float(initial)
    if state.get('started_utc'):
        out['started_utc'] = str(state['started_utc'])
    if state.get('last_utc'):
        out['last_utc'] = str(state['last_utc'])
    marks = state.get('marks')
    if isinstance(marks, dict):
        out['marks'] = {str(k): float(v) for k, v in marks.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
    history = state.get('history')
    if isinstance(history, list):
        out['history'] = history[-500:]
    returns = state.get('active_returns')
    if isinstance(returns, list):
        out['active_returns'] = [float(r) for r in returns[-250:]
                                 if isinstance(r, (int, float)) and not isinstance(r, bool)]
    return out


def load(path):
    """Return the stored account, or ``None`` if absent, unreadable or inconsistent."""
    if not path:
        return None
    p = Path(path)
    try:
        if not p.is_file():
            return None
        return _validate(json.loads(p.read_text('utf-8')))
    except (OSError, ValueError, TypeError):
        return None


def save(path, state):
    """Atomically persist the account. A failure is reported, never swallowed silently."""
    if not path:
        return False
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=p.parent, prefix='paper-', suffix='.tmp',
                                         encoding='utf-8', delete=False) as f:
            tmp = Path(f.name)
            json.dump(state, f, indent=2, sort_keys=True, default=str)
            f.write('\n')
        os.replace(tmp, p)
        tmp = None
        return True
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def open_account(path, initial_nav=100000.0):
    """Load the account, or start one at ``initial_nav`` with a stated reason."""
    state = load(path)
    if state is not None:
        return state, False
    return {'nav': float(initial_nav), 'weights': {}, 'cash': 1.0, 'periods': 0,
            'history': [], 'active_returns': []}, True
