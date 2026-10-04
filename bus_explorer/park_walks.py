"""Orchestrates the 'Park Walks' flow:

1. Look up the named park directly in OpenStreetMap, across Greater London
   (overpass_client.find_parks_by_name) - unlike every other search in this
   app, Park Walks needs the park's actual boundary geometry (area.py), not
   just a representative point, to size it and find its centroid.
2. Filter to plausibly public, decent-sized parks (area_filter.is_walkable_park,
   area_filter.MIN_AREA_M2) and keep the largest same-named match if more
   than one survives (e.g. a park split across several OSM elements).
3. Generate a real, path-following loop route via OpenRouteService
   (routing_client.py), started from the park's centroid and hard-
   constrained with an avoid_polygon covering everything outside the park,
   so the route is physically unable to leave it (see
   _park_loop_route/_avoid_polygon_outside_park). There's no user-chosen
   target distance any more - see _candidate_loop_lengths_m for how a
   sensible length is picked from the park's own size instead.
4. Persist the result (db.upsert_park_walk), cached per park name so
   re-viewing the same search doesn't re-call the routing engine.

This is deliberately a simpler, coarser version of "plan a walking route"
than a dedicated routing app would give: perimeter/centroid-based sizing is
an approximation (see area.py's docstrings), not a proper park-paths-aware
router - consistent with this app's established "not GIS-grade, good
enough" approach elsewhere (area.py, car_explorer.py's circular bbox, etc).

Earlier versions of this module searched for parks near a typed starting
place and joined two parks together if one alone wasn't big enough for a
user-chosen target distance (5/10/15km). Both the starting-place search and
the distance choice were dropped in turn - this module now just takes a
park's own name and returns one route that stays inside it, sized to
whatever that specific park's own paths can support.
"""

import math
import re

from . import area, area_filter, db, overpass_client, routing_client

# How far beyond a park's own mapped boundary the avoid-polygon's "hole"
# (see _avoid_polygon_outside_park) is padded, so a real path that runs
# right along - or marginally outside, from ordinary OSM tracing
# imprecision - the boundary isn't itself excluded from routing.
AVOID_POLYGON_MARGIN_M = 150

# ORS's avoid_polygons has a practical vertex budget - a big relation-based
# park (e.g. assembled from many short OSM ways) can have a boundary ring
# with hundreds of points, far more precision than this hole actually
# needs. Rings longer than this are decimated (see _simplify_ring) before
# being sent.
MAX_AVOID_RING_POINTS = 200

# A loop that stays inside a park can't realistically exceed that park's
# own perimeter by much - a walk of the full perimeter is the natural
# "biggest sensible loop" for a given park, now that there's no user-chosen
# target distance to aim for instead. Smaller fallbacks below that are
# tried in turn (see _candidate_loop_lengths_m) in case the park's actual
# mapped path network can't support a loop that long while staying inside
# the avoid_polygon constraint.
_LOOP_LENGTH_FRACTIONS = [1.0, 0.6, 0.35, 0.15]

# Below this, a loop isn't worth generating (ORS's round_trip needs a
# meaningfully positive length anyway).
MIN_LOOP_M = 300


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


def _simplify_ring(ring, max_points=MAX_AVOID_RING_POINTS):
    """Decimates ring to at most max_points by taking every Nth point - a
    rougher but still closed approximation of the same shape, cheap enough
    for ORS's avoid_polygons to accept for even a very detailed boundary.
    Not needed (and not applied) for rings already within the budget.
    """
    if len(ring) <= max_points:
        return ring
    stride = math.ceil(len(ring) / max_points)
    simplified = ring[::stride]
    if simplified[-1] != ring[-1]:
        simplified.append(ring[-1])
    return simplified


def _avoid_polygon_outside_park(ring, margin_m=AVOID_POLYGON_MARGIN_M):
    """A GeoJSON Polygon-with-hole covering the area around park's ring but
    not the ring's own interior - passed to ORS as options.avoid_polygons
    so a round-trip loop (see _park_loop_route) is physically unable to
    leave the park via the surrounding street grid, rather than merely
    being nudged inward by its start point.

    The exterior is a bounding box around the park padded by margin_m; the
    hole is the park's own boundary, also padded by margin_m so a real path
    running right along (or marginally outside, from ordinary OSM tracing
    imprecision) the mapped boundary isn't itself excluded. Any road or
    path outside the bounding box is unreachable anyway, since it would
    have to cross the avoided donut in between to get there.
    """
    ring = _simplify_ring(ring)
    padded_ring = _pad_ring_outward(ring, margin_m)

    # Exterior ring: a bounding box around the *padded* ring (plus a small
    # extra buffer) rather than the original - so it's guaranteed to fully
    # contain the hole below regardless of the park's shape (a purely
    # radial pad from the centroid doesn't grow every point's bounding box
    # by exactly margin_m in each axis - e.g. for a long, thin park - so
    # sizing the exterior off the original ring's bbox could leave the
    # hole poking outside it at the tips, which is invalid GeoJSON).
    lats = [p[0] for p in padded_ring]
    lons = [p[1] for p in padded_ring]
    mean_lat = sum(lats) / len(lats)
    extra_pad_m = 50
    lat_pad = extra_pad_m / 111_320
    lon_pad = extra_pad_m / (111_320 * max(math.cos(math.radians(mean_lat)), 0.01))
    min_lat, max_lat = min(lats) - lat_pad, max(lats) + lat_pad
    min_lon, max_lon = min(lons) - lon_pad, max(lons) + lon_pad

    # Exterior ring: wound counterclockwise in (lon, lat) order - GeoJSON's
    # convention for an outer ring.
    exterior = [
        [min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat],
    ]

    # Hole: the park's own padded boundary, wound clockwise - the opposite
    # of the exterior, GeoJSON's convention for an interior ring. The
    # source ring's winding direction depends on how its OSM way was
    # drawn, so it's only reversed if it isn't already clockwise.
    hole = [[lon, lat] for lat, lon in padded_ring]
    if hole[0] != hole[-1]:
        hole.append(hole[0])
    if area.ring_is_ccw(padded_ring):
        hole.reverse()

    return {"type": "Polygon", "coordinates": [exterior, hole]}


def _pad_ring_outward(ring, margin_m):
    """Pushes each point of ring outward from the ring's own centroid by
    margin_m - a simple, non-GIS-grade boundary dilation (consistent with
    this app's other flat-earth approximations - see area.py), good enough
    to give real boundary-hugging paths a little breathing room without
    needing a proper polygon-buffer library.
    """
    centroid = area.ring_centroid(ring)
    lat_scale = 111_320
    lon_scale = 111_320 * max(math.cos(math.radians(centroid[0])), 0.01)
    padded = []
    for lat, lon in ring:
        dy = (lat - centroid[0]) * lat_scale
        dx = (lon - centroid[1]) * lon_scale
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            padded.append((lat, lon))
            continue
        scale = (dist + margin_m) / dist
        padded.append((centroid[0] + dy * scale / lat_scale, centroid[1] + dx * scale / lon_scale))
    return padded


def _candidate_loop_lengths_m(ring):
    """Lengths (in m, longest first) worth trying for park's loop - see
    _LOOP_LENGTH_FRACTIONS for why the park's own perimeter is the basis
    for these rather than a user-chosen target distance. Deduplicated and
    floored at MIN_LOOP_M, since a small park's 0.15-fraction step can
    otherwise fall below what round_trip will accept.
    """
    perimeter = area.ring_perimeter_m(ring)
    lengths = sorted({max(MIN_LOOP_M, round(perimeter * f)) for f in _LOOP_LENGTH_FRACTIONS}, reverse=True)
    return lengths


def _park_loop_route(park):
    """A round-trip loop through park, as long as its own mapped paths can
    support while staying inside it.

    Started from the park's centroid rather than its boundary, and
    constrained with avoid_polygon (see _avoid_polygon_outside_park) so the
    whole loop - not just its first and last stretch - is physically unable
    to leave the park. ORS's routing graph has no notion that the start
    point is a park, only a coordinate: a bare round_trip from the
    boundary, and later just a centroid start with no avoid_polygon, were
    both tried first and measured to let the route skirt the park on the
    surrounding streets to make up a requested length, since round_trip's
    length-targeting has no concept of staying inside an area at all.

    Tries _candidate_loop_lengths_m in turn (longest first) and returns the
    first that succeeds - a park's own perimeter is a reasonable upper
    estimate for "the biggest sensible loop", but if its actual mapped path
    network is sparser than that (common for smaller or less-detailed OSM
    parks), ORS's round_trip can't satisfy the longer candidates without
    leaving the avoided area and raises RoutingError for those, which this
    just treats as "try a shorter one" rather than a hard failure. Only
    raises (to the caller) if every candidate, down to MIN_LOOP_M, fails.
    """
    centroid = park["centroid"]
    avoid_polygon = _avoid_polygon_outside_park(park["ring"])

    last_error = None
    for length_m in _candidate_loop_lengths_m(park["ring"]):
        try:
            return routing_client.round_trip_route(
                centroid[0], centroid[1], length_m, avoid_polygon=avoid_polygon
            )
        except routing_client.RoutingError as e:
            last_error = e
    raise routing_client.RoutingError(
        f"Could not fit any walking loop inside {park['name']} - its mapped paths may be too sparse."
    ) from last_error


def find_park_walk(park_name):
    parks = _load_parks_by_name(park_name)
    if not parks:
        raise ParkNotFoundError(f"Could not find a park called '{park_name}'.")

    # More than one element can match the same name (e.g. a park split
    # across several OSM ways) - the largest is the best guess at "the real
    # park", same approach as park_walks.py's old _dedupe_parks.
    park = max(parks, key=lambda p: p["area_m2"])

    route = _park_loop_route(park)

    search_key = _search_key(park_name)
    row = {
        "search_key": search_key,
        "park_name": park["name"],
        "distance_m": route["distance_m"],
        "duration_s": route["duration_s"],
        "start_lat": route["coordinates"][0][0],
        "start_lon": route["coordinates"][0][1],
        "route_geometry": [[lat, lon] for lat, lon in route["coordinates"]],
        "instructions": [{"label": f"Walk around {park['name']}", "steps": route.get("instructions", [])}],
    }
    db.upsert_park_walk(row)

    return {"search_key": search_key, "park_name": park["name"]}
