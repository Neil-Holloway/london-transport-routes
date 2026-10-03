"""Orchestrates the 'Explore by car' flow:

1. Geocode the starting place to coordinates (geocode).
2. Draw a circle of max_radius_m around it and query OpenStreetMap once for
   candidates inside that circle (overpass_client).
3. Categorise, score and deduplicate candidates (categorise, scoring),
   exactly as Explore does for a bus route.
4. Enrich the surviving candidates with a short description (enrich).
5. Persist attractions + their distance from the start (db).

Deliberately much simpler than pipeline.py (Explore) or whatcanido.py (What
Can I Do): a car has no stop, no line, no direction to walk from, so there's
nothing to attribute a candidate to except the starting point itself - this
is a plain radius search, not a stop-based one. Reuses Explore's heritage
category list (categorise.py) rather than What Can I Do's activity list,
since "things worth a drive to see" is the same kind of question Explore
already answers for a bus route, just without a route.
"""

import math
import re
import time

from . import area_filter, categorise, db, enrich, geocode, overpass_client, scoring


class PlaceNotFoundError(Exception):
    pass


# A car search's bbox is a circle around an arbitrary point, not a narrow
# strip hugging a bus route - at radii as modest as 5 miles this routinely
# surfaces 500+ surviving candidates (measured against Beckenham). Two
# separate costs scale with that count and both risk exceeding gunicorn's
# 180s worker timeout (surfaced to the user as a plain "Internal Server
# Error"):
#   1. area_filter.filter_by_area's extra Overpass geometry query and
#      scoring.dedupe's pairwise comparisons, which run over every
#      surviving candidate regardless of how many are ultimately enriched.
#   2. enrich()'s sequential network calls (Wikipedia, Wikidata, a
#      web-search fallback) per surviving candidate, at roughly half a
#      second each - measured: enriching 541 candidates took 284s alone.
# MAX_PRE_FILTER caps (1)'s input before the costly area/dedupe steps even
# run; MAX_ENRICHED then caps (2) after dedupe. Both trim the long tail of
# least-notable candidates from this particular search; those places can
# still turn up via Explore or What Can I Do, since attractions are shared
# across all three searches.
MAX_PRE_FILTER = 300
MAX_ENRICHED = 75


def _log(label, start):
    print(f"[car_explorer] {label}: {time.time() - start:.1f}s elapsed", flush=True)


def _search_key(place_name):
    return re.sub(r"\s+", " ", place_name.strip().lower())


def _bounding_box(lat, lon, radius_m):
    lat_pad = radius_m / 111_320
    lon_pad = radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    return (lat - lat_pad, lon - lon_pad, lat + lat_pad, lon + lon_pad)


def find_by_car(place_name, radius_m):
    t0 = time.time()
    coords = geocode.geocode_place(place_name)
    if not coords:
        raise PlaceNotFoundError(f"Could not find a place called '{place_name}'.")
    origin_lat, origin_lon = coords
    _log("geocode", t0)

    bbox = _bounding_box(origin_lat, origin_lon, radius_m)
    raw_candidates = overpass_client.find_candidates(*bbox)
    _log(f"overpass fetch ({len(raw_candidates)} raw)", t0)

    candidates = []
    for raw in raw_candidates:
        tags = raw["tags"]
        dist = scoring.haversine_m(origin_lat, origin_lon, raw["lat"], raw["lon"])
        if dist > radius_m:
            continue  # the bbox is a rectangle; keep the search itself circular
        category = categorise.categorise(tags)
        total_score = scoring.score_candidate(tags, dist, radius_m)
        if total_score < scoring.MIN_SCORE:
            continue
        candidates.append(
            {
                "osm_type": raw["osm_type"],
                "osm_id": raw["osm_id"],
                "name": tags["name"],
                "lat": raw["lat"],
                "lon": raw["lon"],
                "tags": tags,
                "category": category,
                "score": total_score,
                "intrinsic_score": scoring.intrinsic_score(tags),
                "distance_m": dist,
            }
        )

    # Cap *before* the area-geometry and dedupe passes below, not just
    # before enrich() - a dense bbox can leave hundreds of candidates here,
    # and both of those steps scale with however many are passed in. A
    # couple of genuinely interesting but low-scoring places just outside
    # the cap may be missed; see MAX_PRE_FILTER's comment above.
    candidates.sort(key=lambda c: -c["score"])
    candidates = candidates[:MAX_PRE_FILTER]
    _log(f"score/filter ({len(candidates)} kept)", t0)

    # Parks/gardens/nature reserves are only worth a special trip if they're
    # a decent size - see area_filter.py.
    sized_candidates = area_filter.filter_by_area(candidates)
    _log(f"area_filter ({len(sized_candidates)} kept)", t0)

    deduped = scoring.dedupe(sized_candidates)
    _log(f"dedupe ({len(deduped)} kept)", t0)

    # Cap to the best-scoring candidates before the expensive enrich() pass
    # (see MAX_ENRICHED), then switch to distance order for display/storage.
    deduped.sort(key=lambda c: -c["score"])
    deduped = deduped[:MAX_ENRICHED]
    deduped.sort(key=lambda c: c["distance_m"])

    search_key = _search_key(place_name)

    attraction_rows = []
    car_attraction_rows = []
    for cand in deduped:
        attraction_id = f"osm:{cand['osm_type']}:{cand['osm_id']}"
        text = enrich.enrich(cand["tags"], cand["category"], cand["lat"], cand["lon"])
        address = geocode.address_from_tags(cand["tags"])

        attraction_rows.append(
            {
                "id": attraction_id,
                "name": cand["name"],
                "category": cand["category"],
                "lat": cand["lat"],
                "lon": cand["lon"],
                "why": text["why"],
                "history": text["history"],
                "source_url": text["source_url"],
                "address": address,
                "osm_type": cand["osm_type"],
                "osm_id": cand["osm_id"],
            }
        )
        car_attraction_rows.append(
            {
                "search_key": search_key,
                "attraction_id": attraction_id,
                "distance_m": cand["distance_m"],
                "max_radius_m": radius_m,
            }
        )

    # One connection, and one round trip per table - see pipeline.py /
    # whatcanido.py for why this matters once a search returns more than a
    # handful of candidates.
    _log(f"enrich ({len(deduped)} candidates)", t0)

    with db.get_conn() as conn:
        db.clear_car_attractions(search_key, conn=conn)
        db.bulk_upsert_attractions(attraction_rows, conn=conn)
        db.bulk_upsert_car_attractions(car_attraction_rows, conn=conn)
    _log("db write", t0)

    return {"search_key": search_key, "place_name": place_name, "count": len(deduped)}
