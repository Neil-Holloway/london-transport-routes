"""Builds a walking order and printable itinerary for a selected set of
attractions along a bus route.

The ordering is a simple nearest-neighbour walk: start at whichever selected
attraction sits earliest along the bus route (lowest sequence_index), then
repeatedly hop to whichever remaining attraction is physically closest. This
isn't a true shortest-path solver, but for a handful of on-foot stops it
produces a sensible, non-backtracking route without the complexity of a real
TSP solution.
"""

from . import scoring

WALK_SPEED_M_PER_MIN = 80  # matches pipeline.py's walking pace assumption


def build_plan(attractions):
    """attractions: list of attraction dicts (must include lat, lon; may
    include sequence_index to pick a sensible starting point).

    Returns (plan, totals) where plan is the attractions in walking order,
    each annotated with distance_from_prev_m / walk_minutes_from_prev, and
    totals summarises the whole walk.
    """
    if not attractions:
        return [], {"total_distance_m": 0, "total_walk_minutes": 0, "stop_count": 0}

    remaining = sorted(attractions, key=lambda a: a.get("sequence_index") or 0)
    current = remaining.pop(0)
    ordered = [current]
    while remaining:
        remaining.sort(
            key=lambda a: scoring.haversine_m(current["lat"], current["lon"], a["lat"], a["lon"])
        )
        current = remaining.pop(0)
        ordered.append(current)

    plan = []
    total_distance = 0.0
    prev = None
    for idx, stop in enumerate(ordered, start=1):
        dist = 0.0 if prev is None else scoring.haversine_m(prev["lat"], prev["lon"], stop["lat"], stop["lon"])
        total_distance += dist
        plan.append(
            {
                **stop,
                "stop_number": idx,
                "distance_from_prev_m": dist,
                "walk_minutes_from_prev": round(dist / WALK_SPEED_M_PER_MIN, 1),
            }
        )
        prev = stop

    totals = {
        "total_distance_m": round(total_distance),
        "total_walk_minutes": round(total_distance / WALK_SPEED_M_PER_MIN),
        "stop_count": len(plan),
    }
    return plan, totals
