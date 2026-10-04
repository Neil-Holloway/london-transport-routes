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


def ring_is_ccw(points):
    """True if the ring winds counterclockwise in (lon, lat) order - the
    convention GeoJSON (RFC 7946) uses to tell an exterior ring from a
    hole (used by park_walks._avoid_polygon_outside_park to orient the
    "hole" cut out of its avoid-polygon correctly). This is a planar
    winding-direction property of the raw lon/lat coordinates, not a
    physical-distance one, so it's computed directly rather than via
    _project's metre projection - same shoelace formula as ring_area_m2,
    just unprojected and keeping its sign.
    """
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:] + points[:1]):
        total += lon1 * lat2 - lon2 * lat1
    return total > 0


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


def ring_centroid(points):
    """Area-weighted centroid of a closed (or implicitly-closed) ring of
    (lat, lon) points - the standard polygon centroid formula, projected
    the same way as ring_area_m2. Falls back to a simple
    average of the points for degenerate rings (fewer than 3 points, or
    zero/near-zero area, e.g. a self-intersecting or sliver boundary)
    where the area-weighted formula divides by ~0.
    """
    if len(points) < 3:
        lat = sum(p[0] for p in points) / len(points)
        lon = sum(p[1] for p in points) / len(points)
        return (lat, lon)

    mean_lat = sum(p[0] for p in points) / len(points)
    mean_lon = sum(p[1] for p in points) / len(points)
    lat_scale = 111_320
    lon_scale = 111_320 * max(math.cos(math.radians(mean_lat)), 0.01)
    projected = _project(points)

    signed_area = 0.0
    cx = 0.0
    cy = 0.0
    for (x1, y1), (x2, y2) in zip(projected, projected[1:] + projected[:1]):
        cross = x1 * y2 - x2 * y1
        signed_area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    signed_area /= 2

    if abs(signed_area) < 1e-6:
        lat = sum(p[0] for p in points) / len(points)
        lon = sum(p[1] for p in points) / len(points)
        return (lat, lon)

    cx /= 6 * signed_area
    cy /= 6 * signed_area
    return (mean_lat + cy / lat_scale, mean_lon + cx / lon_scale)


def element_ring(el):
    """el is a raw Overpass element from an 'out geom' query (way or
    relation). Returns a single representative boundary ring - a list of
    (lat, lon) points - or [] if it has no usable geometry.

    For a way, that's just its own geometry. For a relation (multipolygon
    park), picks the largest-area 'outer' member rather than stitching every
    member into one ring - mirrors element_area_m2's outer/inner handling,
    and is good enough for "roughly where is this park's edge" (perimeter/
    centroid/nearest-point purposes) without needing a proper multipolygon
    assembly for parks that are split into several outer ways (e.g. around
    an inlet or a road crossing the boundary).
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
