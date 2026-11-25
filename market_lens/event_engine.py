"""Political & economic historical event search, Monte Carlo outcome prediction, and Kelly sizing."""
import statistics
from alpha_engine import kelly_fraction
from paper_trader import monte_carlo_confidence

def search_events(events, query=''):
    """Search historical political/economic events by keyword or tag."""
    q = query.casefold().split()
    if not q:
        return events
    out = []
    for ev in events:
        text = ' '.join([ev.get('title', ''), ev.get('category', ''), ev.get('outcome', ''), ' '.join(ev.get('tags', []))]).casefold()
        if all(w in text for w in q):
            out.append(ev)
    return out

def predict_event_outcome(events, query='', trials=500):
    """Find matching historical events, run Monte Carlo on their returns, and compute Kelly sizing."""
    matched = search_events(events, query)
    rets = [float(ev['forward_return']) for ev in matched if ev.get('forward_return') is not None]
    if not rets:
        return {'matched_count': len(matched), 'kelly_size': 0.0, 'monte_carlo': {'ok': False}}
    mc = monte_carlo_confidence(rets, trials=trials) if len(rets) >= 6 else {'ok': True, 'mean': statistics.mean(rets), 'ci_95': [min(rets), max(rets)], 'prob_beat_benchmark': sum(1 for r in rets if r > 0) / len(rets)}
    var = statistics.variance(rets) if len(rets) >= 2 else 0.04
    k = kelly_fraction(statistics.mean(rets), var, fraction=0.5, max_weight=0.40)
    return {'matched_count': len(matched), 'mean_return': round(statistics.mean(rets), 6), 'kelly_size': k, 'monte_carlo': mc}
