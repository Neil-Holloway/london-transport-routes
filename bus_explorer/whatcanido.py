"""Orchestrates the 'What Can I Do' flow (spec section 4):

1. Geocode the starting place to coordinates (geocode).
2. Find bus stops within walking distance of it (tfl_client).
3. For each stop, for each route serving it, take the stop sequence in
   both directions from that boarding stop (tfl_client).
4. Query OpenStreetMap once for activity candidates across the combined
   bounding box of every reachable stop (overpass_client).
5. Categorise candidates (categorise_activity), attribute each to its
   nearest reachable stop/line/direction, and persist (db).

Deliberately reuses the same attractions/visits tables as Bus Explorer's
heritage search - an activity place has the same kind of identity (an OSM
element) and the same per-visitor visited/favourite model, it's just found
and described differently. See the What Can I Do spec, sections 2 and 9.
"""

import math
import re

from . import categorise_activity, db, geocode, overpass_client, scoring, tfl_client

WALK_SPEED_M_PER_MIN = 80  # matches pipeline.py's walking pace assumption


class PlaceNotFoundError(Exception):
    pass


def _search_key(place_name):
    return re.sub(r"\s+", " ", place_name.strip().lower())


def _bounding_box(points, pad_m):
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    mean_lat = sum(lats) / len(lats)
    lat_pad = pad_m / 111_320
    lon_pad = pad_m / (111_320 * max(math.cos(math.radians(mean_lat)), 0.01))
    return (min(lats) - lat_pad, min(lons) - lon_pad, max(lats) + lat_pad, max(lons) + lon_pad)


def _nearest_journey_stop(lat, lon, journeys):
    """Across every journey's stop list, find the nearest stop to (lat, lon)
    and which journey (line/direction) it belongs to.

    Returns (journey, stop, distance_m) or (None, None, None) if there are
    no journeys at all.
    """
    best = None
    for journey in journeys:
        for stop in journey["stops"]:
            d = scoring.haversine_m(lat, lon, stop["lat"], stop["lon"])
            if best is None or d < best[2]:
                best = (journey, stop, d)
    return best if best else (None, None, None)


def find_activities(place_name, max_walk_to_stop_m, max_walk_from_stop_m, selected_categories=None):
    coords = geocode.geocode_place(place_name)
    if not coords:
        raise PlaceNotFoundError(f"Could not find a place called '{place_name}'.")
    origin_lat, origin_lon = coords

    nearby_stops = tfl_client.find_nearby_stops(origin_lat, origin_lon, max_walk_to_stop_m)
    if not nearby_stops:
        raise PlaceNotFoundError(
            f"No bus stops found within {max_walk_to_stop_m} m of '{place_name}'."
        )

    journeys = []
    for stop in nearby_stops:
        for line_id in stop["lines"]:
            journeys.extend(
                {"line_id": line_id, **journey}
                for journey in tfl_client.journeys_from_stop(line_id, stop["id"])
            )

    if not journeys:
        raise PlaceNotFoundError(f"No onward bus journeys found from stops near '{place_name}'.")

    # One small bbox per bus line (both directions combined), not one bbox
    # enclosing every journey from every line - a well-connected place's
    # routes can fan out in very different directions, and a single combined
    # bbox ends up covering huge swathes of area nowhere near any actual
    # reachable stop (see overpass_client._build_multi_bbox_query). Grouping
    # by line rather than by individual journey keeps the query to a
    # manageable size (one place can have dozens of journeys but far fewer
    # distinct lines); it's safe to be this coarse because candidates are
    # still filtered against each one's actual nearest-stop distance below -
    # this step only needs to fetch a superset that's cheap to query.
    stops_by_line = {}
    for journey in journeys:
        stops_by_line.setdefault(journey["line_id"], []).extend(journey["stops"])
    bboxes = list({_bounding_box(stops, max_walk_from_stop_m) for stops in stops_by_line.values()})
    raw_candidates = overpass_client.find_candidates_multi_bbox(
        bboxes, clauses=overpass_client.ACTIVITY_QUERY_CLAUSES
    )

    candidates = []
    for raw in raw_candidates:
        tags = raw["tags"]
        category = categorise_activity.categorise_activity(tags)
        if category is None:
            continue
        if selected_categories and category not in selected_categories:
            continue
        journey, stop, dist = _nearest_journey_stop(raw["lat"], raw["lon"], journeys)
        if journey is None or dist > max_walk_from_stop_m:
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
                "intrinsic_score": scoring.intrinsic_score(tags),
                "line_id": journey["line_id"],
                "direction": journey["direction"],
                "destination_stop_name": stop["name"],
                "distance_m": dist,
            }
        )

    deduped = scoring.dedupe(candidates)
    deduped.sort(key=lambda c: c["distance_m"])

    search_key = _search_key(place_name)

    attraction_rows = []
    journey_rows = []
    for cand in deduped:
        attraction_id = f"osm:{cand['osm_type']}:{cand['osm_id']}"
        tags = cand["tags"]
        source_url = tags.get("website") or (
            f"https://www.openstreetmap.org/{cand['osm_type']}/{cand['osm_id']}"
        )
        address = geocode.address_from_tags(tags)

        attraction_rows.append(
            {
                "id": attraction_id,
                "name": cand["name"],
                "category": cand["category"],
                "lat": cand["lat"],
                "lon": cand["lon"],
                "why": categorise_activity.DESCRIPTIONS.get(cand["category"]),
                "history": None,
                "source_url": source_url,
                "address": address,
                "osm_type": cand["osm_type"],
                "osm_id": cand["osm_id"],
            }
        )
        journey_rows.append(
            {
                "search_key": search_key,
                "attraction_id": attraction_id,
                "line_id": cand["line_id"],
                "direction": cand["direction"],
                "destination_stop_name": cand["destination_stop_name"],
                "distance_m": cand["distance_m"],
                "walk_minutes": round(cand["distance_m"] / WALK_SPEED_M_PER_MIN, 1),
                "max_walk_to_stop_m": max_walk_to_stop_m,
                "max_walk_from_stop_m": max_walk_from_stop_m,
            }
        )

    # One connection, and one round trip per table, rather than one
    # connection per row - a well-connected place (e.g. Lewisham: ~25 bus
    # lines) can have hundreds of surviving candidates, and even with a
    # shared connection, one DB round trip per row was slow enough in
    # aggregate to blow gunicorn's worker timeout on top of the Overpass
    # fetch that comes before it.
    with db.get_conn() as conn:
        db.clear_journey_attractions(search_key, conn=conn)
        db.bulk_upsert_attractions(attraction_rows, conn=conn)
        db.bulk_upsert_journey_attractions(journey_rows, conn=conn)

    return {
        "search_key": search_key,
        "place_name": place_name,
        "count": len(deduped),
    }
