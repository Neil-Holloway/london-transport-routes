"""Area calculation for OSM way/relation geometry - see area_filter.py.

Projects lat/lon to local metres around each ring's own mean latitude
(same flat-earth approximation used elsewhere in this codebase - see
pipeline._bounding_box / whatcanido._bounding_box) and applies the
shoelace formula. This is deliberately not GIS-grade (no true spherical
area, no handling of rings crossing the antimeridian) - London-sized
parks are small enough relative to the Earth's radius that the flat
approximation's error is negligible, and the >10ha threshold this feeds
only needs to be roughly right, not exact (see the What Can I Do spec,
section 10, on this kind of search already being a rougher judgement
than a proper GIS dataset).
"""

import math


def _project(points):
    """points: list of (lat, lon). Returns list of (x, y) in metres,
    relative to the ring's own mean latitude/longitude.
    """
    mean_lat = sum(p[0] for p in points) / len(points)
    mean_lon = sum(p[1] for p in points) / len(points)
    lat_scale = 111_320
    lon_scale = 111_320 * max(math.cos(math.radians(mean_lat)), 0.01)
    return [((lon - mean_lon) * lon_scale, (lat - mean_lat) * lat_scale) for lat, lon in points]


def ring_area_m2(points):
    """Shoelace formula on a closed (or implicitly-closed) ring of
    (lat, lon) points. Returns a positive area regardless of winding
    direction. Returns 0.0 for anything that can't be a polygon (fewer
    than 3 points).
    """
    if len(points) < 3:
        return 0.0
    projected = _project(points)
    total = 0.0
    for (x1, y1), (x2, y2) in zip(projected, projected[1:] + projected[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def point_distance_m(p1, p2):
    """Straight-line distance between two (lat, lon) points, in metres -
    projects both together around their shared mean latitude (same
    approach as ring_area_m2). Used by build_open_spaces.py's parkrun
    matching as a fallback when a parkrun start point doesn't fall inside
    any park's mapped boundary (see point_in_ring).
    """
    (x1, y1), (x2, y2) = _project([p1, p2])
    return math.hypot(x2 - x1, y2 - y1)


def point_in_ring(point, ring):
    """Ray-casting point-in-polygon test. point is a (lat, lon) pair,
    ring a list of (lat, lon) points forming a closed (or implicitly-
    closed) boundary. Used by build_open_spaces.py to test whether a
    parkrun start point or trail vertex falls inside a park's boundary
    (see element_ring below for how that boundary is obtained) - not used
    for the GeoJSON borough polygons, which have their own copy of this
    in borough.py since GeoJSON coordinates are (lon, lat) and can have
    holes.
    """
    if len(ring) < 3:
        return False
    lat, lon = point
    inside = False
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[1:] + ring[:1]):
        if (lon1 > lon) != (lon2 > lon):
            lat_intersect = lat1 + (lon - lon1) / (lon2 - lon1) * (lat2 - lat1)
            if lat < lat_intersect:
                inside = not inside
    return inside


def _point_to_segment_distance_m(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def min_distance_point_to_ring_m(point, ring):
    """Shortest distance from a single (lat, lon) point to any edge of a
    closed (or implicitly-closed) ring, in metres. Projects the point
    together with the ring around the ring's own mean latitude/longitude
    (same approach as ring_area_m2), so the point and the ring's segments
    end up in the same local projected frame rather than two separate
    ones. Used by build_open_spaces.py to decide whether a trail passes
    close enough to a park's boundary to count as "visited".

    Returns float('inf') for a ring with fewer than 2 points (no edges to
    measure against).
    """
    if len(ring) < 2:
        return float("inf")
    projected = _project(list(ring) + [point])
    ring_proj, p = projected[:-1], projected[-1]
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(ring_proj, ring_proj[1:] + ring_proj[:1]):
        best = min(best, _point_to_segment_distance_m(p, (x1, y1), (x2, y2)))
    return best


def element_ring(el):
    """el is a raw Overpass element from an 'out geom' query (way or
    relation). Returns a single representative boundary ring - a list of
    (lat, lon) points - or [] if it has no usable geometry.

    For a way, that's just its own geometry. For a relation (multipolygon
    park), picks the largest-area 'outer' member rather than stitching
    every member into one ring - good enough for "roughly where is this
    park's edge" (point-in-ring / nearest-distance purposes, see
    point_in_ring and min_distance_point_to_ring_m above) without needing
    a proper multipolygon assembly for parks split into several outer
    ways (e.g. around an inlet or a road crossing the boundary).
    """
    if el["type"] == "way":
        geometry = el.get("geometry") or []
        return [(pt["lat"], pt["lon"]) for pt in geometry if pt]

    if el["type"] == "relation":
        best_ring = []
        best_area = -1.0
        for member in el.get("members", []):
            if member.get("role") == "inner":
                continue
            geometry = member.get("geometry") or []
            points = [(pt["lat"], pt["lon"]) for pt in geometry if pt]
            if len(points) < 3:
                continue
            candidate_area = ring_area_m2(points)
            if candidate_area > best_area:
                best_area = candidate_area
                best_ring = points
        return best_ring

    return []


def element_area_m2(el):
    """el is a raw Overpass element from an 'out geom' query (way or
    relation). Returns the element's area in m^2, or 0.0 if it has no
    usable geometry (e.g. a relation with no way members, or a node).

    Relations (multipolygon parks) are handled by summing 'outer' member
    ways' areas and subtracting 'inner' ones (lakes/islands excluded from
    a park's own area) - an approximation of a proper multipolygon area
    that's good enough for a >10ha threshold, without needing to stitch
    disconnected way segments into a single ring first.
    """
    if el["type"] == "way":
        geometry = el.get("geometry") or []
        points = [(pt["lat"], pt["lon"]) for pt in geometry if pt]
        return ring_area_m2(points)

    if el["type"] == "relation":
        total = 0.0
        for member in el.get("members", []):
            geometry = member.get("geometry") or []
            points = [(pt["lat"], pt["lon"]) for pt in geometry if pt]
            if len(points) < 3:
                continue
            ring = ring_area_m2(points)
            if member.get("role") == "inner":
                total -= ring
            else:
                total += ring
        return max(total, 0.0)

    return 0.0
