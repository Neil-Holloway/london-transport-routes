"""Orchestrates the 'Park Walks' flow:

1. Geocode the starting place to coordinates (geocode).
2. Query OpenStreetMap for park/garden/nature_reserve ways and relations
   within walking catchment of it, with full boundary geometry, not just a
   representative point (overpass_client.find_parks_with_geometry) - unlike
   every other search in this app, Park Walks needs each candidate's actual
   shape (area.py) to size it and find a point on its edge, not just "is it
   near enough".
3. Filter to plausibly public, decent-sized parks (area_filter.is_walkable_park,
   area_filter.MIN_AREA_M2) and keep the nearest one to the starting point as
   the walk's anchor park.
4. Decide whether that one park is big enough to plausibly absorb the whole
   requested walk, or whether it needs joining to a second nearby park (see
   _is_single_park_feasible).
5. Generate a real, path-following route via OpenRouteService
   (routing_client.py) - a loop from the anchor park's nearest edge point
   for the single-park case, or an explicit waypoint route through both
   parks for the joined case.
6. Persist the result (db.upsert_park_walk), cached per (place, target
   distance) so re-viewing the same search doesn't re-call the routing
   engine.

This is deliberately a simpler, coarser version of "plan a walking route"
than a dedicated routing app would give: perimeter-as-capacity and
nearest-ring-vertex-as-entrance are approximations (see area.py's
docstrings), not a proper park-paths-aware router - consistent with this
app's established "not GIS-grade, good enough" approach elsewhere (area.py,
car_explorer.py's circular bbox, etc).
"""

import math
import re

from . import area, area_filter, db, geocode, overpass_client, routing_client, scoring

# How far someone is assumed willing to walk from the starting place just to
# reach the anchor park, before any of the requested walk distance itself -
# same purpose as max_walk_to_station_m in train_explorer.py, just for a
# park gate instead of a station.
DEFAULT_MAX_WALK_TO_PARK_M = 2000

# A loop that stays roughly within one park can't exceed that park's own
# perimeter by much - SLACK_FACTOR allows for some extra back-and-forth on
# the park's internal paths beyond just tracing the outer edge once, without
# letting an obviously too-small park be used for a walk several times its
# own size (at which point the route would mostly leave the park anyway,
# which is exactly what joining to a second park is for).
SLACK_FACTOR = 1.5

# Below this, a per-park loop isn't worth generating as its own leg - see
# _build_joined_route.
MIN_LOOP_M = 300

# How many ring-vertex "spokes" out to the park's centroid and back a loop
# uses - see _park_loop_waypoints. Capped so a large target distance against
# a small park doesn't build an unreasonably long waypoint list for one ORS
# call.
MAX_LOOP_SPOKES = 6

TARGET_DISTANCES_M = [5000, 10000, 15000]


class PlaceNotFoundError(Exception):
    pass


def _search_key(place_name):
    return re.sub(r"\s+", " ", place_name.strip().lower())


def _bounding_box(lat, lon, radius_m):
    lat_pad = radius_m / 111_320
    lon_pad = radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    return (lat - lat_pad, lon - lon_pad, lat + lat_pad, lon + lon_pad)


def _load_parks(min_lat, min_lon, max_lat, max_lon):
    """Fetch and filter candidate parks in a bounding box. Returns a list of
    dicts: {name, tags, ring, area_m2, perimeter_m, centroid}. Unnamed
    elements and anything without a usable boundary ring are dropped - both
    can't usefully be shown to the user or sized.
    """
    raw_elements = overpass_client.find_parks_with_geometry(min_lat, min_lon, max_lat, max_lon)

    parks = []
    for el in raw_elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name or not area_filter.is_walkable_park(tags):
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
                "name": name,
                "tags": tags,
                "ring": ring,
                "area_m2": area_m2,
                "perimeter_m": area.ring_perimeter_m(ring),
                "centroid": area.ring_centroid(ring),
            }
        )
    return parks


def _dedupe_parks(parks):
    """The same park can appear as both a way and a relation, or be split
    across more than one element with the same name - keep the
    largest-area element per name, same purpose as scoring.dedupe but
    simpler, since parks don't need score-based ranking, just one surviving
    copy per real-world place.
    """
    best = {}
    for park in parks:
        key = park["name"].strip().lower()
        if key not in best or park["area_m2"] > best[key]["area_m2"]:
            best[key] = park
    return list(best.values())


def _is_single_park_feasible(park, target_distance_m):
    return park["perimeter_m"] * SLACK_FACTOR >= target_distance_m


def _park_loop_waypoints(park, target_length_m):
    """Builds an explicit waypoint sequence for a loop that starts and ends
    at the park's entrance and repeatedly dips into its centroid.

    A bare ORS round_trip from the entrance alone was tried first and
    measured to just skirt the park on the surrounding streets rather than
    actually walking through it - ORS's routing graph has no notion that
    the start point is a park, only a coordinate, and if the park's own
    path network is thinner than the streets around it (true for many
    smaller London parks in OSM), the street grid wins on cost every time.
    Forcing the route through the centroid - a point ORS can only reach via
    paths genuinely inside the park - fixes that, and doing it in multiple
    spokes rather than once lets the loop's length scale with
    target_length_m (more, evenly-spaced spokes for a longer target) instead
    of being a fixed shape regardless of what's being asked for.
    """
    entrance = park["entrance"]
    centroid = park["centroid"]
    ring = park["ring"]

    radius_m = scoring.haversine_m(entrance[0], entrance[1], centroid[0], centroid[1])
    if radius_m < 1:
        return [entrance, centroid, entrance]

    spokes = max(1, min(MAX_LOOP_SPOKES, round(target_length_m / (2 * radius_m))))

    entrance_index = area.nearest_ring_index(ring, entrance[0], entrance[1])
    step = max(1, len(ring) // (spokes + 1))

    waypoints = [entrance]
    for n in range(1, spokes + 1):
        waypoints.append(centroid)
        waypoints.append(ring[(entrance_index + n * step) % len(ring)])
    waypoints.append(centroid)
    waypoints.append(entrance)
    return waypoints


def _park_loop_route(park, target_length_m):
    return routing_client.directions_route(_park_loop_waypoints(park, target_length_m))


def _build_joined_route(park1, park2, target_distance_m):
    """Compose a joined two-park route out of three real ORS calls, rather
    than one explicit-waypoint directions_route straight through both parks
    - that was tried first and measured to just take whatever the shortest
    real path between the waypoints happened to be (e.g. ~2.6-3.9km against
    a requested 5/10/15km, identical regardless of target_distance_m), since
    plain directions has no concept of a target length at all. Here, only
    the leg between the two parks (a fixed, real distance - can't be
    stretched) is a single directions_route call; the two loops inside each
    park are each their own centroid-forcing waypoint route (see
    _park_loop_route), sized so leg + loop1 + loop2 + leg (walking the join
    both ways) adds up to roughly target_distance_m.

    Returns None if the parks are too far apart to leave a meaningful
    length for either park's loop - the caller falls back to a single-park
    route at the full target distance in that case.
    """
    leg = routing_client.directions_route([park1["entrance"], park2["entrance"]])
    d_leg = leg["distance_m"] or 0

    remaining = target_distance_m - 2 * d_leg
    if remaining < 2 * MIN_LOOP_M:
        return None

    total_perim = park1["perimeter_m"] + park2["perimeter_m"]
    l1 = max(MIN_LOOP_M, remaining * park1["perimeter_m"] / total_perim)
    l2 = max(MIN_LOOP_M, remaining - l1)

    loop1 = _park_loop_route(park1, l1)
    loop2 = _park_loop_route(park2, l2)

    coordinates = (
        loop1["coordinates"]
        + leg["coordinates"]
        + loop2["coordinates"]
        + list(reversed(leg["coordinates"]))
    )
    # The return leg isn't routed separately - ORS's foot-walking paths are
    # assumed symmetric in each direction, so the outbound leg's distance/
    # duration is just doubled rather than making a 4th API call. There are
    # therefore no real turn-by-turn steps for it either - a single
    # "retrace your steps" instruction stands in for it below, rather than
    # presenting the outbound leg's steps a second time as if they were
    # freshly computed for the return.
    distance_m = (loop1["distance_m"] or 0) + (loop2["distance_m"] or 0) + 2 * d_leg
    duration_s = (loop1["duration_s"] or 0) + (loop2["duration_s"] or 0) + 2 * (leg["duration_s"] or 0)
    sections = [
        {"label": f"Walk around {park1['name']}", "steps": loop1["instructions"]},
        {"label": f"Walk to {park2['name']}", "steps": leg["instructions"]},
        {"label": f"Walk around {park2['name']}", "steps": loop2["instructions"]},
        {
            "label": "Walk back to the start",
            "steps": [
                {
                    "instruction": f"Retrace your steps back towards {park1['name']} and the start.",
                    "distance_m": d_leg,
                    "duration_s": leg["duration_s"],
                }
            ],
        },
    ]
    return {"coordinates": coordinates, "distance_m": distance_m, "duration_s": duration_s, "sections": sections}


def find_park_walk(place_name, target_distance_m, max_walk_to_park_m=DEFAULT_MAX_WALK_TO_PARK_M):
    coords = geocode.geocode_place(place_name)
    if not coords:
        raise PlaceNotFoundError(f"Could not find a place called '{place_name}'.")
    origin_lat, origin_lon = coords

    # Search wide enough to plausibly find a second park to join to for the
    # largest target distance, not just the nearest park to the start -
    # scaled to the walk itself rather than a fixed radius, since a 15km
    # walk's candidate parks can reasonably be further out than a 5km one's.
    search_radius_m = max(max_walk_to_park_m, target_distance_m * 0.6)
    bbox = _bounding_box(origin_lat, origin_lon, search_radius_m)
    parks = _dedupe_parks(_load_parks(*bbox))

    for park in parks:
        park["entrance"] = area.nearest_point_on_ring(park["ring"], origin_lat, origin_lon)
        park["distance_from_start_m"] = scoring.haversine_m(
            origin_lat, origin_lon, park["entrance"][0], park["entrance"][1]
        )

    reachable = [p for p in parks if p["distance_from_start_m"] <= max_walk_to_park_m]
    if not reachable:
        raise PlaceNotFoundError(
            f"No sufficiently large park found within {max_walk_to_park_m} m of '{place_name}'."
        )
    reachable.sort(key=lambda p: p["distance_from_start_m"])
    park1 = reachable[0]

    other_parks = [p for p in parks if p is not park1]

    route = None
    mode = "single"
    park_names = park1["name"]

    if not _is_single_park_feasible(park1, target_distance_m) and other_parks:
        park2 = min(
            other_parks,
            key=lambda p: scoring.haversine_m(
                park1["centroid"][0], park1["centroid"][1], p["centroid"][0], p["centroid"][1]
            ),
        )
        route = _build_joined_route(park1, park2, target_distance_m)
        if route is not None:
            mode = "joined"
            park_names = f"{park1['name']} + {park2['name']}"

    if route is None:
        # Either park1 alone is big enough, there's no other park to join
        # to, or the nearest other park is too far away to leave a
        # meaningful length for either park's loop (see
        # _build_joined_route) - a single-park loop at the full requested
        # distance is the best remaining option.
        route = _park_loop_route(park1, target_distance_m)

    # _build_joined_route already returns its own multi-leg "sections";
    # a single-park loop is just the one leg.
    sections = route.get("sections") or [
        {"label": f"Walk around {park1['name']}", "steps": route.get("instructions", [])}
    ]

    search_key = _search_key(place_name)
    row = {
        "search_key": search_key,
        "target_distance_m": target_distance_m,
        "place_name": place_name,
        "mode": mode,
        "park_names": park_names,
        "distance_m": route["distance_m"],
        "duration_s": route["duration_s"],
        "start_lat": origin_lat,
        "start_lon": origin_lon,
        "route_geometry": [[lat, lon] for lat, lon in route["coordinates"]],
        "instructions": sections,
    }
    db.upsert_park_walk(row)

    return {"search_key": search_key, "place_name": place_name, "target_distance_m": target_distance_m}
