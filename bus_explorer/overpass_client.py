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

# Tag values for What Can I Do's activity search (categorise_activity.py) -
# a deliberately different set of "interesting" tags, since the question
# being answered (what can I do) is different from Bus Explorer's (what's
# worth discovering). See the What Can I Do spec, section 5.
ACTIVITY_QUERY_CLAUSES = [
    '["amenity"="cinema"]',
    '["amenity"="library"]',
    '["amenity"="theatre"]',
    '["amenity"="marketplace"]',
    '["shop"="market"]',
    '["leisure"="swimming_pool"]',
    '["leisure"="bowling_green"]',
    '["leisure"~"^(sports_centre|fitness_centre)$"]',
    '["landuse"="allotments"]',
    '["landuse"="recreation_ground"]',
]


def _build_query(min_lat, min_lon, max_lat, max_lon, clauses):
    bbox = f"({min_lat},{min_lon},{max_lat},{max_lon})"
    parts = []
    for clause in clauses:
        parts.append(f"node{clause}{bbox};")
        parts.append(f"way{clause}{bbox};")
    body = "\n".join(parts)
    return f"[out:json][timeout:50];\n({body}\n);\nout center tags;"


def _build_multi_bbox_query(bboxes, clauses):
    """Like _build_query, but for several bounding boxes in one query -
    e.g. one small box per bus journey, rather than one box enclosing every
    journey combined (which balloons to the size of the widest-spread
    journeys and ends up querying huge swathes of empty area between
    unrelated routes - see What Can I Do spec, section 4 performance note).
    """
    parts = []
    for min_lat, min_lon, max_lat, max_lon in bboxes:
        bbox = f"({min_lat},{min_lon},{max_lat},{max_lon})"
        for clause in clauses:
            parts.append(f"node{clause}{bbox};")
            parts.append(f"way{clause}{bbox};")
    body = "\n".join(parts)
    return f"[out:json][timeout:50];\n({body}\n);\nout center tags;"


def _execute(query, rounds=1, timeout=25):
    """POST query to each mirror in turn, trying every mirror before
    retrying any of them again - a mirror that's briefly overloaded gets a
    second chance only after the others have already been tried, rather
    than burning the retry budget hammering the same slow mirror twice in a
    row. `timeout` is deliberately shorter than Overpass's own
    [timeout:50] query budget so a stuck mirror fails fast enough to leave
    time for `rounds` > 1 to actually help within the app's own request
    timeout, rather than one hung attempt eating the whole budget.
    """
    body = urllib.parse.urlencode({"data": query}).encode()

    last_error = None
    for _ in range(rounds):
        for mirror in MIRRORS:
            try:
                req = urllib.request.Request(
                    mirror, data=body, headers={"User-Agent": USER_AGENT}
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
                return _normalise_elements(data.get("elements", []))
            except OSError as e:
                # Covers HTTPError/URLError plus socket.timeout, which on
                # Python <3.10 is a distinct class from TimeoutError and
                # would otherwise escape uncaught, skipping remaining
                # mirrors/retries and crashing the request.
                last_error = e
                time.sleep(2)
    raise RuntimeError(f"Overpass query failed on all mirrors: {last_error}")


def find_candidates(min_lat, min_lon, max_lat, max_lon, retries_per_mirror=1, clauses=None):
    """Query Overpass for candidate POIs in a bounding box.

    clauses defaults to INTERESTING_QUERY_CLAUSES (Bus Explorer's heritage
    search); pass ACTIVITY_QUERY_CLAUSES for What Can I Do.

    Returns a list of dicts: {id, type, lat, lon, tags}.
    """
    query = _build_query(min_lat, min_lon, max_lat, max_lon, clauses or INTERESTING_QUERY_CLAUSES)
    return _execute(query, rounds=retries_per_mirror)


def find_candidates_multi_bbox(bboxes, retries_per_mirror=2, clauses=None):
    """Query Overpass for candidate POIs across several bounding boxes in a
    single request. See _build_multi_bbox_query for why this exists.

    bboxes is a list of (min_lat, min_lon, max_lat, max_lon) tuples.
    Defaults to two rounds through all mirrors (see _execute) - What Can I
    Do's combined-bbox queries are heavier than a single-route heritage
    search and were seen failing in production against mirrors that worked
    fine from elsewhere, so a single pass per mirror wasn't resilient enough.

    Returns a deduplicated list of dicts: {id, type, lat, lon, tags} - the
    same OSM element can legitimately fall inside more than one journey's
    box (routes often share stretches of road).
    """
    query = _build_multi_bbox_query(bboxes, clauses or INTERESTING_QUERY_CLAUSES)
    elements = _execute(query, rounds=retries_per_mirror)

    seen = set()
    deduped = []
    for el in elements:
        key = (el["osm_type"], el["osm_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(el)
    return deduped


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
