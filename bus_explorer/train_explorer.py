"""Orchestrates the 'Explore by train' flow:

1. Geocode the starting place to coordinates (geocode).
2. Find national rail/Overground/Elizabeth line stations within walking
   distance of it (tfl_client.find_nearby_rail_stations) - deliberately not
   tfl_client.find_nearby_stops, which excludes national rail.
3. For each station, for each operator line serving it, take the onward
   stop sequence in both directions from that boarding station, capped to
   max_stops (tfl_client.journeys_from_stop) - a mainline rail branch's real
   terminus can be dozens of stops and many miles away (e.g. Southeastern
   through Beckenham Junction reaching Ramsgate), further than a "quick trip
   from this station" search should cover.
4. Query OpenStreetMap once for candidates across the combined bounding box
   of every reachable station (overpass_client), exactly as What Can I Do
   does for a bus stop.
5. Categorise, score and deduplicate candidates (categorise, scoring),
   exactly as Explore does for a bus route - reuses Explore's heritage
   category list and enrich() rather than What Can I Do's activity list,
   since "things worth the train ride to see" is the same kind of question
   Explore already answers for a bus route, just from a station instead of
   a stop.
6. Enrich the surviving candidates with a short description (enrich).
7. Persist attractions + their per-journey placement (db).
"""

import re
from collections import defaultdict

from . import area_filter, categorise, db, enrich, geocode, overpass_client, scoring, tfl_client
from .whatcanido import WALK_SPEED_M_PER_MIN, _bounding_box, _nearest_journey_stop


class PlaceNotFoundError(Exception):
    pass


# A train search's bboxes enclose every stop on every onward branch from
# every nearby station (see _bounding_box below) - for a station with
# several operators and several branches each (e.g. Beckenham Junction:
# Southeastern/Southern/Thameslink, 8 branches just for Southeastern), that
# can enclose a surprisingly wide area despite the modest stop cap. Measured
# against Beckenham at 1.5km/5 stops: 613 candidates survived scoring/dedupe
# - enriching all of them (~0.5s each, see car_explorer.py's measurement)
# would take ~5 minutes, well past gunicorn's 180s worker timeout. MAX_ENRICHED
# caps that, per category (see _quota_cap) so no single heavily-tagged
# category (Memorials and monuments, Churches - the two biggest groups in
# that measurement) crowds out smaller ones.
MAX_ENRICHED = 150


def _quota_cap(items, total_cap, categories):
    """Cap items to total_cap, split roughly evenly across categories rather
    than by raw score alone - see car_explorer._quota_cap, which this mirrors
    (parameterised on the category list, since Explore's heritage list has
    18 categories rather than car's restricted 4).
    """
    by_category = defaultdict(list)
    for item in items:
        by_category[item["category"]].append(item)
    for group in by_category.values():
        group.sort(key=lambda c: -c["score"])

    n = len(categories)
    base_quota, remainder = divmod(total_cap, n)
    quotas = {cat: base_quota + (1 if i < remainder else 0) for i, cat in enumerate(categories)}

    selected = []
    leftover = []
    for cat in categories:
        group = by_category.get(cat, [])
        quota = quotas[cat]
        selected.extend(group[:quota])
        leftover.extend(group[quota:])

    shortfall = total_cap - len(selected)
    if shortfall > 0:
        leftover.sort(key=lambda c: -c["score"])
        selected.extend(leftover[:shortfall])
    return selected


def _search_key(place_name):
    return re.sub(r"\s+", " ", place_name.strip().lower())


def find_by_train(place_name, max_walk_to_station_m, max_stops):
    coords = geocode.geocode_place(place_name)
    if not coords:
        raise PlaceNotFoundError(f"Could not find a place called '{place_name}'.")
    origin_lat, origin_lon = coords

    nearby_stations = tfl_client.find_nearby_rail_stations(
        origin_lat, origin_lon, max_walk_to_station_m
    )
    if not nearby_stations:
        raise PlaceNotFoundError(
            f"No railway stations found within {max_walk_to_station_m} m of '{place_name}'."
        )

    journeys = []
    for station in nearby_stations:
        for line_id in station["lines"]:
            journeys.extend(
                {"line_id": line_id, **journey}
                for journey in tfl_client.journeys_from_stop(
                    line_id, station["id"], max_stops=max_stops
                )
            )

    if not journeys:
        raise PlaceNotFoundError(f"No onward journeys found from stations near '{place_name}'.")

    # One small bbox per line (both directions combined), not one bbox
    # enclosing every journey from every line - same reasoning as
    # whatcanido.find_activities: a well-connected station's operators can
    # fan out in very different directions, and a single combined bbox ends
    # up covering huge swathes of area nowhere near any actual reachable
    # station.
    stops_by_line = {}
    for journey in journeys:
        stops_by_line.setdefault(journey["line_id"], []).extend(journey["stops"])
    bboxes = list({_bounding_box(stops, max_walk_to_station_m) for stops in stops_by_line.values()})
    raw_candidates = overpass_client.find_candidates_multi_bbox(bboxes)

    candidates = []
    for raw in raw_candidates:
        tags = raw["tags"]
        category = categorise.categorise(tags)
        journey, stop, dist = _nearest_journey_stop(raw["lat"], raw["lon"], journeys)
        if journey is None or dist > max_walk_to_station_m:
            continue
        total_score = scoring.score_candidate(tags, dist, max_walk_to_station_m)
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
                "line_id": journey["line_id"],
                "direction": journey["direction"],
                "destination_stop_name": stop["name"],
                "distance_m": dist,
            }
        )

    # Parks/gardens/nature reserves are only worth a special trip if they're
    # a decent size - see area_filter.py.
    sized_candidates = area_filter.filter_by_area(candidates)

    deduped = scoring.dedupe(sized_candidates)

    # Cap to the best-scoring candidates before the expensive enrich() pass
    # (see MAX_ENRICHED), per category (see _quota_cap), then switch to
    # distance order for display/storage.
    deduped = _quota_cap(deduped, MAX_ENRICHED, categorise.CATEGORIES)
    deduped.sort(key=lambda c: c["distance_m"])

    search_key = _search_key(place_name)

    attraction_rows = []
    train_attraction_rows = []
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
        train_attraction_rows.append(
            {
                "search_key": search_key,
                "attraction_id": attraction_id,
                "line_id": cand["line_id"],
                "direction": cand["direction"],
                "destination_stop_name": cand["destination_stop_name"],
                "distance_m": cand["distance_m"],
                "walk_minutes": round(cand["distance_m"] / WALK_SPEED_M_PER_MIN, 1),
                "max_walk_to_station_m": max_walk_to_station_m,
                "max_stops": max_stops,
            }
        )

    # One connection, and one round trip per table - see pipeline.py /
    # whatcanido.py for why this matters once a search returns more than a
    # handful of candidates.
    with db.get_conn() as conn:
        db.clear_train_attractions(search_key, conn=conn)
        db.bulk_upsert_attractions(attraction_rows, conn=conn)
        db.bulk_upsert_train_attractions(train_attraction_rows, conn=conn)

    return {"search_key": search_key, "place_name": place_name, "count": len(deduped)}
