"""Read-only capture filter; never a motion permission or an arrival check."""

import math


def stationary(history, now):
    """Require fresh, finite joint samples spanning one second within 0.15 deg."""
    rows = [(t, q) for t, q in history if now - 1.5 <= t <= now]
    if len(rows) < 5 or now - rows[-1][0] > .25:
        return False
    if rows[-1][0] - rows[0][0] < 1.:
        return False
    if any(b[0] - a[0] > .25 or b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        return False
    if any(len(q) != 4 or not all(math.isfinite(x) for x in q) for _, q in rows):
        return False
    return all(max(q[i] for _, q in rows) - min(q[i] for _, q in rows)
               <= math.radians(.15) for i in range(4))
