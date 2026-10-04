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
