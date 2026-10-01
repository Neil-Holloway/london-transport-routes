"""Interest scoring and deduplication for candidate places.

The scoring formula is intentionally simple for Milestone 0.1 and is expected
to be tuned experimentally later (per spec section 10). The user never sees
the numeric score — it's only used to rank/filter internally.
"""

import math

MIN_SCORE = 1.0  # candidates below this are dropped as too thin on information
DEDUP_RADIUS_M = 40  # candidates within this distance + similar name are merged


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def intrinsic_score(tags):
    """Score based only on the place itself - independent of any particular
    route or how close it happens to be. Used to pick the canonical element
    when deduplicating, so the same real-world place resolves to the same
    attraction regardless of which route someone explores it from.
    """
    score = 0.0

    if tags.get("wikipedia") or tags.get("wikidata"):
        score += 3.0  # information quality + usually a proxy for real significance
    if tags.get("historic"):
        score += 2.0
    if tags.get("tourism") in ("museum", "gallery", "artwork", "viewpoint"):
        score += 2.0
    if tags.get("memorial"):
        score += 1.5
    if tags.get("description") or tags.get("inscription"):
        score += 1.0

    # Unusualness: a rough proxy - anything that isn't a generic park/church
    # gets a small bump, since those are comparatively common.
    if tags.get("historic") not in (None,) and tags.get("historic") not in ("yes",):
        score += 0.5

    return score


def score_candidate(tags, distance_to_route_m, max_walk_m):
    """Total score used for filtering/ranking within a specific route's
    results: intrinsic interest plus a proximity bonus that tapers to 0 at
    max_walk_m.
    """
    score = intrinsic_score(tags)
    proximity_fraction = max(0.0, (max_walk_m - distance_to_route_m) / max_walk_m)
    score += proximity_fraction * 2.0
    return score


def _normalise_name(name):
    return "".join(ch.lower() for ch in name if ch.isalnum())


def _is_linear_feature(cand):
    """Rivers, canals etc. are mapped in OSM as many separate way segments
    that can be far apart along a route, unlike a single point-like place
    accidentally tagged twice nearby. For these we dedupe by name alone,
    ignoring distance, since a bus route running alongside "River X" for
    a kilometre shouldn't surface a dozen identical "River X" cards.
    """
    tags = cand.get("tags", {})
    return bool(tags.get("waterway")) or cand.get("category") == "Rivers, canals and waterways"


def dedupe(candidates):
    """candidates: list of dicts with 'name', 'lat', 'lon', 'score',
    'intrinsic_score', 'tags', 'category', etc.

    Keeps the candidate with the highest intrinsic (route-independent) score
    among near-duplicates: same normalised name, and (for ordinary point-like
    places) within DEDUP_RADIUS_M of each other. Linear features such as
    rivers/canals are merged by name alone regardless of distance, since a
    single real-world river is typically split into many OSM way segments
    running the length of a route.
    """
    kept = []
    for cand in sorted(candidates, key=lambda c: -c["intrinsic_score"]):
        cand_norm = _normalise_name(cand["name"])
        is_dup = False
        for existing in kept:
            if _normalise_name(existing["name"]) != cand_norm:
                continue
            if _is_linear_feature(cand) and _is_linear_feature(existing):
                is_dup = True
                break
            if haversine_m(cand["lat"], cand["lon"], existing["lat"], existing["lon"]) <= DEDUP_RADIUS_M:
                is_dup = True
                break
        if not is_dup:
            kept.append(cand)
    return kept
