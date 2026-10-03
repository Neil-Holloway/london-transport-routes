"""Overpass API client for finding candidate points of interest near a route.

Public Overpass instances are shared, rate-limited infrastructure and can be
slow or briefly unavailable, so we try a couple of mirrors with retries.

Mirror list ordering matters for this specific deployment: direct testing
from Render (see the former /debug-overpass diagnostic route, removed once
this was confirmed) found overpass-api.de and lz4.overpass-api.de (both
Hetzner-hosted) are completely unreachable from Render's network - instant
"Network is unreachable", not a slow/overloaded response - while several
other public mirrors (kumi.systems, openstreetmap.ru, mail.ru, osm.vi-di.fr)
are reachable but stall for 15+ seconds on even a trivial single-node
query, suggesting they're rate-limiting or deprioritising Render's egress
IPs specifically. overpass.osm.ch was the only mirror that responded
quickly from Render - but it turned out to be a Switzerland-scoped data
extract with zero London/UK coverage (confirmed directly: a Bromley-area
library query that returns 5 real results on the Hetzner mirrors returns 0
on osm.ch), so it's excluded entirely rather than just deprioritised -
a "successful" response from it is worse than a failure, since it looks
like a legitimately empty result instead of an error. overpass.openstreetmap.fr
leads the list instead: it has correct UK data and isn't Hetzner-hosted, so
it's a plausible candidate for being reachable from Render where the
Hetzner mirrors aren't, though this hasn't yet been confirmed against
Render's network specifically (only from a non-Render network). The
overpass.openstreetmap.fr was confirmed reachable from Render (Bickley and
Petts Wood both succeeded), so the Hetzner mirrors have now been dropped
entirely rather than just deprioritised: keeping proven-dead mirrors in the
rotation wasn't just wasted retry time, it was actively misleading - once
openstreetmap.fr itself failed or timed out, the error shown to the user was
always whichever Hetzner mirror failed last (always "Network is
unreachable"), masking the real reason openstreetmap.fr didn't answer. That
masking is exactly what happened with Lewisham: a major interchange's
"What Can I Do" query spans ~25 distinct bus lines (one bbox per line, see
_build_multi_bbox_query), producing a ~55KB Overpass query that legitimately
takes 50-60+ seconds for the server to compute - far past the old 25s
per-attempt timeout - so it timed out on the one real mirror and fell
through to the dead ones, surfacing a "network unreachable" error that had
nothing to do with the actual problem (see find_candidates_multi_bbox's
longer default timeout below).
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MIRRORS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
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
    # The nonce comment keeps a retried query from being byte-for-byte
    # identical to the previous attempt - some mirrors (e.g. overpass.osm.ch)
    # reject an exact-duplicate query sent again shortly after the first with
    # a "duplicate_query" error instead of running it, which would otherwise
    # make retries (either ours or a user re-submitting the same search)
    # pointless.
    return f"[out:json][timeout:50];\n// nonce:{uuid.uuid4()}\n({body}\n);\nout center tags;"


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
    # See _build_query for why the nonce comment is here.
    return f"[out:json][timeout:50];\n// nonce:{uuid.uuid4()}\n({body}\n);\nout center tags;"


def _execute(query, rounds=1, timeout=25):
    """POST query to each mirror in turn, trying every mirror before
    retrying any of them again - a mirror that's briefly overloaded gets a
    second chance only after the others have already been tried, rather
    than burning the retry budget hammering the same slow mirror twice in a
    row. `timeout` should comfortably cover how long the query actually
    takes to compute server-side (see find_candidates_multi_bbox for why
    that varies a lot by query size) - too short and a mirror that would
    have answered correctly just gets abandoned mid-computation.
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
            except (OSError, ValueError) as e:
                # OSError covers HTTPError/URLError plus socket.timeout, which
                # on Python <3.10 is a distinct class from TimeoutError and
                # would otherwise escape uncaught, skipping remaining
                # mirrors/retries and crashing the request. ValueError covers
                # json.JSONDecodeError - some mirrors return an HTML/XML error
                # body instead of JSON (e.g. a "duplicate_query" rejection),
                # which should also be treated as a retryable mirror failure
                # rather than crashing the request.
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


def find_candidates_multi_bbox(bboxes, retries_per_mirror=2, clauses=None, timeout=65):
    """Query Overpass for candidate POIs across several bounding boxes in a
    single request. See _build_multi_bbox_query for why this exists.

    bboxes is a list of (min_lat, min_lon, max_lat, max_lon) tuples - one per
    distinct bus line reachable from the starting place. A well-connected
    interchange (e.g. Lewisham: ~25 distinct lines) produces a correspondingly
    large combined query that was measured taking 50-60+ seconds for Overpass
    to compute - the default timeout is set well above that so a mirror
    that's genuinely still working isn't abandoned mid-computation and
    silently blamed on "network unreachable" from the mirrors tried after it
    (see the module docstring). Defaults to two attempts (see _execute) so a
    single transient failure doesn't fail the whole search.

    Returns a deduplicated list of dicts: {id, type, lat, lon, tags} - the
    same OSM element can legitimately fall inside more than one journey's
    box (routes often share stretches of road).
    """
    query = _build_multi_bbox_query(bboxes, clauses or INTERESTING_QUERY_CLAUSES)
    elements = _execute(query, rounds=retries_per_mirror, timeout=timeout)

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
