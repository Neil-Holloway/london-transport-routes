"""Orchestrates the 'Park Walks' flow:

1. Look up the named park directly in OpenStreetMap, across Greater London
   (overpass_client.find_parks_by_name) - unlike every other search in this
   app, Park Walks needs the park's actual boundary geometry (area.py), not
   just a representative point, to size it and find its centroid.
2. Filter to plausibly public, decent-sized parks (area_filter.is_walkable_park,
   area_filter.MIN_AREA_M2) and keep the largest same-named match if more
   than one survives (e.g. a park split across several OSM elements).
3. Generate a real, path-following loop route via OpenRouteService
   (routing_client.py), started from the park's centroid so the route
   actually goes through the park rather than skirting it on the
   surrounding streets (see _park_loop_route).
4. Persist the result (db.upsert_park_walk), cached per (park name, target
   distance) so re-viewing the same search doesn't re-call the routing
   engine.

This is deliberately a simpler, coarser version of "plan a walking route"
than a dedicated routing app would give: perimeter/centroid-based sizing is
an approximation (see area.py's docstrings), not a proper park-paths-aware
router - consistent with this app's established "not GIS-grade, good
enough" approach elsewhere (area.py, car_explorer.py's circular bbox, etc).

Earlier versions of this module searched for parks near a typed starting
place, and joined two parks together with a real connecting leg if one park
alone wasn't big enough for the requested distance. That was dropped in
favour of this simpler direct-by-name design - entering a park's own name
and getting a loop walk within just that park.
"""

import re

from . import area, area_filter, db, overpass_client, routing_client

TARGET_DISTANCES_M = [5000, 10000, 15000]


class ParkNotFoundError(Exception):
    pass


def _search_key(park_name):
    return re.sub(r"\s+", " ", park_name.strip().lower())


def _load_parks_by_name(name):
    """Fetch and filter candidate parks matching name. Returns a list of
    dicts: {osm_type, osm_id, name, tags, ring, area_m2, centroid}. Anything
    without a usable boundary ring, or that isn't a plausibly public,
    decent-sized park, is dropped.
    """
    raw_elements = overpass_client.find_parks_by_name(name)

    parks = []
    for el in raw_elements:
        tags = el.get("tags") or {}
        park_name = tags.get("name")
        if not park_name or not area_filter.is_walkable_park(tags):
            continue
        ring = area.element_ring(el)
        if len(ring) < 3:
            continue
        area_m2 = area.element_area_m2(el)
        if area_m2 < area_filter.MIN_AREA_M2:
            continue
        parks.append(
            {
                "osm_type": el["type"],
                "osm_id": el["id"],
                "name": park_name,
                "tags": tags,
                "ring": ring,
                "area_m2": area_m2,
                "centroid": area.ring_centroid(ring),
            }
        )
    return parks


def _park_loop_route(park, target_length_m):
    """A round-trip loop of approximately target_length_m through park.

    Started from the park's centroid rather than its boundary - a bare
    round_trip from the boundary was tried first and measured to just skirt
    the park on the surrounding streets rather than actually walking
    through it. ORS's routing graph has no notion that the start point is a
    park, only a coordinate, so when a park's own path network is thinner
    than the streets right outside it (true for many smaller London parks
    in OSM), round_trip's optimiser can satisfy the requested length
    entirely from the street grid without ever dipping into the park.
    Starting from deep inside instead means every route out of (and back
    to) that point has to use a path genuinely inside the park for at least
    its first and last stretch.
    """
    centroid = park["centroid"]
    return routing_client.round_trip_route(centroid[0], centroid[1], target_length_m)


def find_park_walk(park_name, target_distance_m):
    parks = _load_parks_by_name(park_name)
    if not parks:
        raise ParkNotFoundError(f"Could not find a park called '{park_name}'.")

    # More than one element can match the same name (e.g. a park split
    # across several OSM ways) - the largest is the best guess at "the real
    # park", same approach as park_walks.py's old _dedupe_parks.
    park = max(parks, key=lambda p: p["area_m2"])

    route = _park_loop_route(park, target_distance_m)

    search_key = _search_key(park_name)
    row = {
        "search_key": search_key,
        "target_distance_m": target_distance_m,
        "park_name": park["name"],
        "distance_m": route["distance_m"],
        "duration_s": route["duration_s"],
        "start_lat": route["coordinates"][0][0],
        "start_lon": route["coordinates"][0][1],
        "route_geometry": [[lat, lon] for lat, lon in route["coordinates"]],
        "instructions": [{"label": f"Walk around {park['name']}", "steps": route.get("instructions", [])}],
    }
    db.upsert_park_walk(row)

    return {"search_key": search_key, "park_name": park["name"], "target_distance_m": target_distance_m}
