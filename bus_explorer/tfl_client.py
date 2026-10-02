"""Minimal TfL Unified API client for Bus Explorer.

Resolves a bus route number to its stop sequence (in route order) and the
lat/lon coordinates of every stop.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.tfl.gov.uk"
USER_AGENT = "BusExplorer/0.1 (https://github.com/Georege-Holloway/london-transport-routes)"
APP_KEY = os.environ.get("TFL_APP_KEY")  # optional - raises the anonymous rate limit


class RouteNotFoundError(Exception):
    pass


def _fetch_json(path, retries=5):
    url = f"{API_BASE}{path}"
    if APP_KEY:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}app_key={urllib.parse.quote(APP_KEY)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last_error = e
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after else 2 * attempt)
                continue
        except OSError as e:
            last_error = e
        if attempt < retries:
            time.sleep(0.5 * attempt)  # backoff - TfL rate-limits bursts of requests
    raise RuntimeError(f"Failed to fetch {path}: {last_error}")


_ROUTE_BRANCHES_CACHE = {}


def get_route_branches(line_id):
    """Return ordered branches of stops (id + name), like fetch_routes.py.

    Cached per line_id for the lifetime of the process - a route's stop
    sequence doesn't change minute to minute, and What Can I Do looks the
    same line up repeatedly (once per nearby stop that happens to serve it),
    so without this cache a busy interchange triggers the same full-route
    fetch over and over.
    """
    if line_id in _ROUTE_BRANCHES_CACHE:
        return _ROUTE_BRANCHES_CACHE[line_id]

    seq = _fetch_json(f"/Line/{line_id}/Route/Sequence/all")
    if not seq:
        branches = None
    else:
        branches = []
        for sps in seq.get("stopPointSequences", []):
            branches.append(
                {
                    "direction": sps.get("direction"),
                    "branchId": sps.get("branchId"),
                    "stops": [
                        {
                            "id": sp.get("id"),
                            "name": sp.get("name"),
                            "lat": sp.get("lat"),
                            "lon": sp.get("lon"),
                        }
                        for sp in sps.get("stopPoint", [])
                    ],
                }
            )
    _ROUTE_BRANCHES_CACHE[line_id] = branches
    return branches


_STOP_COORDS_CACHE = {}


def get_stop_coordinates(stop_ids):
    """Fetch lat/lon for a list of stop ids. Returns {id: (lat, lon, name)}.

    Fetched one id at a time and keyed by the *requested* id, not whatever
    id TfL's response reports. This matters because TfL's StopPoint lookup
    sometimes resolves an individual on-street bus stop id to a shared
    "stop pair" group id (e.g. requesting '490014075W' returns a record
    whose own id/naptanId is '490G00014075') - batching by the response's id
    would silently lose the mapping back to the stop we actually asked
    about. The coordinates themselves are still correct for our purposes.

    Cached per stop id for the lifetime of the process - What Can I Do looks
    up the same route (and therefore the same stops) from several nearby
    boarding points, so without this cache every overlapping stop gets
    refetched (at 0.15s/stop) once per boarding point instead of once ever.
    """
    coords = {}
    unique_ids = list(dict.fromkeys(stop_ids))

    for sid in unique_ids:
        if sid in _STOP_COORDS_CACHE:
            if _STOP_COORDS_CACHE[sid] is not None:
                coords[sid] = _STOP_COORDS_CACHE[sid]
            continue

        data = _fetch_json(f"/StopPoint/{urllib.parse.quote(sid)}")
        item = data[0] if isinstance(data, list) and data else data
        result = None
        if item:
            lat, lon, name = item.get("lat"), item.get("lon"), item.get("commonName")
            if lat is not None and lon is not None:
                result = (lat, lon, name)
                coords[sid] = result
        _STOP_COORDS_CACHE[sid] = result
        time.sleep(0.15)

    return coords


def find_nearby_stops(lat, lon, radius_m):
    """Find bus stops within radius_m of (lat, lon), each with the bus
    routes serving it.

    Returns a list of {id, name, lat, lon, lines: [line_id, ...]}. Only
    StopPoints that serve at least one bus route are returned - TfL's radius
    search also returns tube/rail/tram stops, which this app has no use for.
    """
    path = (
        f"/StopPoint?lat={lat}&lon={lon}&radius={radius_m}"
        "&stopTypes=NaptanPublicBusCoachTram&modes=bus"
    )
    data = _fetch_json(path)
    if not data:
        return []

    stops = []
    for sp in data.get("stopPoints", []):
        lines = [
            line["id"]
            for line in sp.get("lines", [])
            if line.get("id")
        ]
        if not lines:
            continue
        stops.append(
            {
                "id": sp.get("naptanId") or sp.get("id"),
                "name": sp.get("commonName"),
                "lat": sp.get("lat"),
                "lon": sp.get("lon"),
                "lines": lines,
            }
        )
    return stops


def _fill_missing_coordinates(branches):
    """Route/Sequence/all already includes lat/lon per stop (see
    get_route_branches), so this should normally be a no-op. It exists only
    as a fallback for the rare stop TfL's sequence response doesn't give
    coordinates for, rather than unconditionally refetching every stop on
    the route one at a time - which used to make a busy area's "What Can I
    Do" search take several minutes (hundreds of individually-throttled
    /StopPoint calls for data already in hand).

    Mutates branches (and therefore the shared route-branches cache) in
    place, so a gap is only ever filled once per line for the life of the
    process.
    """
    missing_ids = [
        s["id"]
        for b in branches
        for s in b["stops"]
        if s["id"] and (s.get("lat") is None or s.get("lon") is None)
    ]
    if not missing_ids:
        return

    coords = get_stop_coordinates(missing_ids)
    for branch in branches:
        for stop in branch["stops"]:
            if stop.get("lat") is None or stop.get("lon") is None:
                c = coords.get(stop["id"])
                if c:
                    stop["lat"], stop["lon"] = c[0], c[1]


def journeys_from_stop(line_id, boarding_stop_id):
    """For a bus route serving a given boarding stop, return the outward
    stop sequence in both directions starting from (and including) that
    stop - i.e. every stop reachable by one bus journey without changing
    buses, in either direction you could board in.

    Returns a list of 0, 1 or 2 dicts: {direction, stops: [{id, name, lat, lon}, ...]}.
    Branches that don't actually serve the boarding stop are skipped (a route
    number can have several branches; only the ones passing through this
    particular stop are relevant to a journey starting here).
    """
    branches = get_route_branches(line_id)
    if not branches:
        return []

    _fill_missing_coordinates(branches)

    journeys = []
    for branch in branches:
        stops = [s for s in branch["stops"] if s["lat"] is not None]
        ids = [s["id"] for s in stops]
        if boarding_stop_id not in ids:
            continue
        idx = ids.index(boarding_stop_id)
        onward = stops[idx:]
        if len(onward) < 2:
            continue  # boarding stop is the end of this branch - nowhere to ride onward to
        journeys.append({"direction": branch["direction"], "stops": onward})

    return journeys


def resolve_route(route_number):
    """Resolve a user-entered route number to branches with coordinates.

    Returns a dict: {
        "line_id": ...,
        "branches": [ { direction, branchId, stops: [{id, name, lat, lon}] } ],
    }
    Raises RouteNotFoundError if the route doesn't exist for London buses.
    """
    line_id = route_number.strip().lower()
    cached_branches = get_route_branches(line_id)
    if not cached_branches:
        raise RouteNotFoundError(f"No London bus route found matching '{route_number}'.")

    _fill_missing_coordinates(cached_branches)

    # Work on copies from here - cached_branches is the shared module-level
    # cache, and callers of resolve_route shouldn't mutate it.
    branches = [{**b, "stops": list(b["stops"])} for b in cached_branches]

    # Drop stops we couldn't geolocate; keep order.
    for branch in branches:
        branch["stops"] = [s for s in branch["stops"] if s["lat"] is not None]

    branches = [b for b in branches if b["stops"]]
    if not branches:
        raise RouteNotFoundError(f"Could not obtain coordinates for route '{route_number}'.")

    return {"line_id": line_id, "branches": branches}


def principal_journey(resolved):
    """Best-effort 'Origin -> Destination' label for the main branch."""
    branches = resolved["branches"]
    main = max(branches, key=lambda b: len(b["stops"]))
    origin = main["stops"][0]["name"]
    destination = main["stops"][-1]["name"]
    return origin, destination
