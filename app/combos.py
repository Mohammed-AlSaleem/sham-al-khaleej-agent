"""Per-user pairing ("combo") memory with time decay.

Two evidence sources, both strictly per guest (nothing is shared across guests):
  observed - items that appeared together in the guest's OWN past orders
  stated   - pairings the guest said they like (combo_events 'like')

A guest can drop a pairing ('drop'). A drop also discards older order evidence for that pair, so the
habit only returns if the guest orders the pair together again afterwards.

Every piece of evidence loses half its weight every COMBO_HALF_LIFE_DAYS, so old habits fade unless repeated.
"""
from itertools import combinations

from . import config, memory, menu


def _decay(ts: str, now) -> float:
    age_days = max((now - memory.parse_ts(ts)).total_seconds() / 86400, 0.0)
    return 0.5 ** (age_days / config.COMBO_HALF_LIFE_DAYS)


def personal_combos(user_id: str, now=None) -> list[dict]:
    """Active pairings for one guest, strongest first. Does NOT apply dietary constraints (tools.py does)."""
    now = now or memory.utcnow()
    state = memory.get_combo_state(user_id)

    co_times: dict[tuple[str, str], list[str]] = {}
    for ts, items in memory.get_order_events(user_id):
        for pair in combinations(sorted(set(items)), 2):
            co_times.setdefault(pair, []).append(ts)

    out = []
    for pair in set(state) | set(co_times):
        if not all(menu.get_item(i) for i in pair):      # item removed from the menu
            continue
        st = state.get(pair)
        drop_ts = st["last_drop_ts"] if st else None
        times = [t for t in co_times.get(pair, []) if drop_ts is None or t > drop_ts]
        observed = (sum(_decay(t, now) for t in times)
                    if len(times) >= config.COMBO_MIN_CO_ORDERS else 0.0)
        stated = (config.COMBO_STATED_WEIGHT * _decay(st["stance_ts"], now)
                  if st and st["stance"] == "like" else 0.0)
        score = observed + stated
        if score < config.COMBO_MIN_SCORE:
            continue
        out.append({
            "items": list(pair),
            "score": round(score, 3),
            "source": "both" if observed and stated else "stated" if stated else "observed",
            "co_orders": len(times),
            "last_ordered": max(times) if times else None,
        })
    out.sort(key=lambda c: -c["score"])
    return out
