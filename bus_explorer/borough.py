"""London borough lookup for the Open Spaces report (see
build_open_spaces.py) - point-in-polygon against a static GeoJSON file of
the 33 London borough boundaries (data/london_boroughs.geojson, sourced
from a free Ordnance-Survey-derived boundary dataset), rather than a live
Overpass admin-boundary query per park.

GeoJSON coordinates are (lon, lat), the opposite order from the (lat, lon)
convention used everywhere else in this codebase (see area.py) - this
module's ray-casting therefore has its own copy rather than reusing
area.point_in_ring, to avoid a mixed-order bug from sharing one function
across both conventions.
"""

import json
import os

_GEOJSON_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "london_boroughs.geojson")

_boroughs = None  # lazily loaded: [(name, [ring, ...]), ...] - one or more
# rings per borough, since a handful of boroughs (e.g. those with
# Thames-side exclaves) may in principle be a MultiPolygon.


def _load():
    global _boroughs
    if _boroughs is not None:
        return _boroughs

    with open(_GEOJSON_PATH, encoding="utf-8") as f:
        data = json.load(f)

    boroughs = []
    for feature in data["features"]:
        name = feature["properties"]["name"]
        geometry = feature["geometry"]
        rings = []
        if geometry["type"] == "Polygon":
            rings.append(geometry["coordinates"][0])  # exterior ring only - London borough
            # boundaries don't have meaningful holes for this purpose.
        elif geometry["type"] == "MultiPolygon":
            for polygon in geometry["coordinates"]:
                rings.append(polygon[0])
        boroughs.append((name, rings))

    _boroughs = boroughs
    return _boroughs


def _point_in_ring(lon, lat, ring):
    inside = False
    for (lon1, lat1), (lon2, lat2) in zip(ring, ring[1:] + ring[:1]):
        if (lat1 > lat) != (lat2 > lat):
            lon_intersect = lon1 + (lat - lat1) / (lat2 - lat1) * (lon2 - lon1)
            if lon < lon_intersect:
                inside = not inside
    return inside


def find_borough(lat, lon):
    """Returns the name of the London borough containing (lat, lon), or
    None if the point doesn't fall inside any of them (e.g. just outside
    the Greater London boundary). First containing borough wins - the 33
    borough boundaries don't overlap, so this is unambiguous.
    """
    for name, rings in _load():
        for ring in rings:
            if _point_in_ring(lon, lat, ring):
                return name
    return None
