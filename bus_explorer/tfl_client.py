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
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
        if attempt < retries:
            time.sleep(0.5 * attempt)  # backoff - TfL rate-limits bursts of requests
    raise RuntimeError(f"Failed to fetch {path}: {last_error}")


def get_route_branches(line_id):
    """Return ordered branches of stops (id + name), like fetch_routes.py."""
    seq = _fetch_json(f"/Line/{line_id}/Route/Sequence/all")
    if not seq:
        return None
    branches = []
    for sps in seq.get("stopPointSequences", []):
        branches.append(
            {
                "direction": sps.get("direction"),
                "branchId": sps.get("branchId"),
                "stops": [
                    {"id": sp.get("id"), "name": sp.get("name")}
                    for sp in sps.get("stopPoint", [])
                ],
            }
        )
    return branches


def get_stop_coordinates(stop_ids):
    """Fetch lat/lon for a list of stop ids. Returns {id: (lat, lon, name)}.

    Fetched one id at a time and keyed by the *requested* id, not whatever
    id TfL's response reports. This matters because TfL's StopPoint lookup
    sometimes resolves an individual on-street bus stop id to a shared
    "stop pair" group id (e.g. requesting '490014075W' returns a record
    whose own id/naptanId is '490G00014075') - batching by the response's id
    would silently lose the mapping back to the stop we actually asked
    about. The coordinates themselves are still correct for our purposes.
    """
    coords = {}
    unique_ids = list(dict.fromkeys(stop_ids))

    for sid in unique_ids:
        data = _fetch_json(f"/StopPoint/{urllib.parse.quote(sid)}")
        item = data[0] if isinstance(data, list) and data else data
        if item:
            lat, lon, name = item.get("lat"), item.get("lon"), item.get("commonName")
            if lat is not None and lon is not None:
                coords[sid] = (lat, lon, name)
        time.sleep(0.15)

    return coords


def resolve_route(route_number):
    """Resolve a user-entered route number to branches with coordinates.

    Returns a dict: {
        "line_id": ...,
        "branches": [ { direction, branchId, stops: [{id, name, lat, lon}] } ],
    }
    Raises RouteNotFoundError if the route doesn't exist for London buses.
    """
    line_id = route_number.strip().lower()
    branches = get_route_branches(line_id)
    if not branches:
        raise RouteNotFoundError(f"No London bus route found matching '{route_number}'.")

    all_stop_ids = [s["id"] for b in branches for s in b["stops"] if s["id"]]
    coords = get_stop_coordinates(all_stop_ids)

    for branch in branches:
        for stop in branch["stops"]:
            c = coords.get(stop["id"])
            if c:
                stop["lat"], stop["lon"] = c[0], c[1]
            else:
                stop["lat"], stop["lon"] = None, None

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
