"""Overpass API client for finding candidate points of interest near a route.

Public Overpass instances are shared, rate-limited infrastructure and can be
slow or briefly unavailable, so we try a couple of mirrors with retries.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

MIRRORS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

USER_AGENT = "BusExplorer/0.1 (https://github.com/Georege-Holloway/london-transport-routes)"

# OSM tag values that make something a plausible "interesting place" candidate.
# Deliberately broad; scoring/categorisation narrows things down later.
INTERESTING_QUERY_CLAUSES = [
    '["historic"]',
    '["tourism"~"^(museum|gallery|artwork|viewpoint|attraction)$"]',
    '["memorial"]',
    '["leisure"~"^(park|garden|nature_reserve)$"]',
    '["waterway"~"^(canal|river|lock)$"]',
    '["man_made"~"^(watermill|windmill|water_tower|gasometer|kiln|lighthouse|pier|crane)$"]',
    '["amenity"="place_of_worship"]',
    '["amenity"="marketplace"]',
    '["landuse"="cemetery"]',
    '["natural"="water"]',
]


def _build_query(min_lat, min_lon, max_lat, max_lon):
    bbox = f"({min_lat},{min_lon},{max_lat},{max_lon})"
    parts = []
    for clause in INTERESTING_QUERY_CLAUSES:
        parts.append(f"node{clause}{bbox};")
        parts.append(f"way{clause}{bbox};")
    body = "\n".join(parts)
    return f"[out:json][timeout:50];\n({body}\n);\nout center tags;"


def find_candidates(min_lat, min_lon, max_lat, max_lon, retries_per_mirror=1):
    """Query Overpass for candidate POIs in a bounding box.

    Returns a list of dicts: {id, type, lat, lon, tags}.
    """
    query = _build_query(min_lat, min_lon, max_lat, max_lon)
    body = urllib.parse.urlencode({"data": query}).encode()

    last_error = None
    for mirror in MIRRORS:
        for attempt in range(retries_per_mirror):
            try:
                req = urllib.request.Request(
                    mirror, data=body, headers={"User-Agent": USER_AGENT}
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                return _normalise_elements(data.get("elements", []))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                last_error = e
                time.sleep(2)
    raise RuntimeError(f"Overpass query failed on all mirrors: {last_error}")


def _normalise_elements(elements):
    out = []
    for el in elements:
        tags = el.get("tags") or {}
        if "name" not in tags:
            continue  # can't usefully present an unnamed feature
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            center = el.get("center") or {}
            lat, lon = center.get("lat"), center.get("lon")
        if lat is None or lon is None:
            continue
        out.append(
            {
                "osm_type": el["type"],
                "osm_id": el["id"],
                "lat": lat,
                "lon": lon,
                "tags": tags,
            }
        )
    return out
