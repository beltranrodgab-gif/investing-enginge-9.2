"""Fail-closed downstream consumer helper for Market Lens 3.7+ exports."""
from decimal import Decimal
from reliability import read_export, FALLBACKS

def load_screened_signals(csv_path, at=None):
    """Return {series_id: Decimal(screened_value)} only for live, unflagged, unexpired rows."""
    rows = read_export(csv_path, at=at)
    signals = {r['series']: Decimal(r['screened_value']) for r in rows}
    for primary, backup in FALLBACKS.items():
        if primary not in signals and backup in signals:
            signals[primary] = signals[backup]
    return signals
