"""Orchestrates Milestone 0.1's 'explore a route' flow:

1. Resolve the route number to ordered stops with coordinates (tfl_client).
2. Query OpenStreetMap for candidate places within the bounding box (overpass_client).
3. Categorise, score and deduplicate candidates (categorise, scoring).
4. Enrich the surviving candidates with a short description (enrich).
5. Work out each candidate's nearest stop, walking distance and position
   along the route.
6. Persist attractions + their per-route placement in SQLite (db).
"""

from . import categorise, db, enrich, geocode, overpass_client, scoring, tfl_client

WALK_SPEED_M_PER_MIN = 80  # ~4.8 km/h, a relaxed walking pace


def _bounding_box(all_stops, pad_m):
    lats = [s["lat"] for s in all_stops]
    lons = [s["lon"] for s in all_stops]
    # ~111,320 m per degree latitude; longitude degree length shrinks with
    # latitude, so scale by cos(latitude) for a reasonable pad in metres.
    import math

    mean_lat = sum(lats) / len(lats)
    lat_pad = pad_m / 111_320
    lon_pad = pad_m / (111_320 * max(math.cos(math.radians(mean_lat)), 0.01))
    return (min(lats) - lat_pad, min(lons) - lon_pad, max(lats) + lat_pad, max(lons) + lon_pad)


def _nearest_stop(lat, lon, ordered_stops):
    best = None
    best_dist = None
    for idx, stop in enumerate(ordered_stops):
        d = scoring.haversine_m(lat, lon, stop["lat"], stop["lon"])
        if best_dist is None or d < best_dist:
            best_dist = d
            best = (idx, stop)
    return best[0], best[1], best_dist


def explore_route(route_number, max_walk_m):
    resolved = tfl_client.resolve_route(route_number)
    line_id = resolved["line_id"]

    # Use the longest branch as the canonical route order for "route order" /
    # nearest-stop-index purposes (Milestone 0.1 doesn't need to model every
    # branch separately).
    main_branch = max(resolved["branches"], key=lambda b: len(b["stops"]))
    ordered_stops = main_branch["stops"]

    bbox = _bounding_box(ordered_stops, max_walk_m)
    raw_candidates = overpass_client.find_candidates(*bbox)

    enriched_candidates = []
    for raw in raw_candidates:
        tags = raw["tags"]
        idx, stop, dist = _nearest_stop(raw["lat"], raw["lon"], ordered_stops)
        if dist > max_walk_m:
            continue
        category = categorise.categorise(tags)
        total_score = scoring.score_candidate(tags, dist, max_walk_m)
        if total_score < scoring.MIN_SCORE:
            continue
        enriched_candidates.append(
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
                "nearest_stop_index": idx,
                "nearest_stop": stop,
                "distance_m": dist,
            }
        )

    deduped = scoring.dedupe(enriched_candidates)
    deduped.sort(key=lambda c: c["nearest_stop_index"])

    results = []
    # One connection shared across the whole batch, rather than opening a
    # fresh Postgres connection per upsert - a route can have dozens of
    # candidates, and Supabase throttles/limits new connections per client
    # in quick succession, which was stalling this loop for minutes
    # (eventually hitting gunicorn's worker timeout) on larger routes.
    with db.get_conn() as conn:
        db.clear_route_attractions(line_id, conn=conn)
        for cand in deduped:
            attraction_id = f"osm:{cand['osm_type']}:{cand['osm_id']}"
            text = enrich.enrich(cand["tags"], cand["category"], cand["lat"], cand["lon"])
            address = geocode.address_from_tags(cand["tags"])

            db.upsert_attraction(
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
                },
                conn=conn,
            )
            db.upsert_route_attraction(
                {
                    "line_id": line_id,
                    "attraction_id": attraction_id,
                    "nearest_stop_id": cand["nearest_stop"]["id"],
                    "nearest_stop_name": cand["nearest_stop"]["name"],
                    "distance_m": cand["distance_m"],
                    "walk_minutes": round(cand["distance_m"] / WALK_SPEED_M_PER_MIN, 1),
                    "sequence_index": cand["nearest_stop_index"],
                    "direction": main_branch["direction"],
                    "max_walk_m": max_walk_m,
                },
                conn=conn,
            )
            results.append(attraction_id)

    origin, destination = tfl_client.principal_journey(resolved)
    return {
        "line_id": line_id,
        "origin": origin,
        "destination": destination,
        "count": len(results),
    }
