"""Minimal TfL Unified API client for Bus Explorer.

Resolves a bus route number to its stop sequence (in route order) and the
lat/lon coordinates of every stop.
"""

import json
import os
import re
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


_NIGHT_BUS_RE = re.compile(r"^n\d")


def _is_night_bus(line_id):
    """TfL's own convention: a night bus's route number is always 'N'
    followed by digits (N199, N1, N3...) - no day route uses this pattern.
    Night routes often run much further/differently from their daytime
    equivalent (see Petts Wood's N199 reaching central London), which isn't
    the kind of local, during-the-day activity trip What Can I Do is for.
    No tube/DLR/Overground/Elizabeth line/tram line id matches this pattern
    (e.g. "northern" starts with 'n' but not 'n' + digit), so applying this
    filter unconditionally to every mode is harmless.
    """
    return bool(_NIGHT_BUS_RE.match(line_id))


_RAIL_MODE_LINE_IDS_CACHE = None


def _rail_mode_line_ids():
    """Line ids for Overground + Elizabeth line - the only two included
    modes that share a NaptanRailStation stop type with excluded
    national/international rail operators (e.g. Stratford's rail station
    lists 'elizabeth' and 'mildmay' alongside 'c2c' and 'greater-anglia' in
    the same stop's lines). Used by find_nearby_stops to filter a
    NaptanRailStation's line list down to just the modes this app covers.

    Fetched once and cached for the lifetime of the process, same pattern
    as get_route_branches - this set is small and essentially static (it
    last changed when Overground's lines were given names in 2024).
    """
    global _RAIL_MODE_LINE_IDS_CACHE
    if _RAIL_MODE_LINE_IDS_CACHE is None:
        data = _fetch_json("/Line/Mode/overground,elizabeth-line") or []
        _RAIL_MODE_LINE_IDS_CACHE = {line["id"] for line in data}
    return _RAIL_MODE_LINE_IDS_CACHE


def find_nearby_stops(lat, lon, radius_m):
    """Find bus/tube/DLR/Overground/Elizabeth line/tram stops within
    radius_m of (lat, lon), each with the routes serving it.

    Returns a list of {id, name, lat, lon, lines: [line_id, ...]}. Only
    StopPoints that serve at least one in-scope, non-night-bus route are
    returned. National rail and international rail (e.g. c2c, Greater
    Anglia, Eurostar) are excluded even though they can share a station
    with Overground/Elizabeth line - see _rail_mode_line_ids.
    """
    path = (
        f"/StopPoint?lat={lat}&lon={lon}&radius={radius_m}"
        "&stopTypes=NaptanPublicBusCoachTram,NaptanMetroStation,NaptanRailStation"
        "&modes=bus,tube,dlr,overground,elizabeth-line,tram"
    )
    data = _fetch_json(path)
    if not data:
        return []

    rail_mode_ids = _rail_mode_line_ids()

    stops = []
    for sp in data.get("stopPoints", []):
        is_rail_station = sp.get("stopType") == "NaptanRailStation"
        lines = []
        for line in sp.get("lines", []):
            line_id = line.get("id")
            if not line_id or _is_night_bus(line_id):
                continue
            if is_rail_station and line_id not in rail_mode_ids:
                continue  # national/international rail sharing this station
            lines.append(line_id)
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


_NON_BUS_LINE_ALIASES_CACHE = None
_NON_BUS_LINE_INFO_CACHE = None


def _fetch_non_bus_lines():
    """Shared fetch behind _non_bus_line_aliases and _non_bus_line_info -
    one /Line/Mode/... call covers both lookups built from it.
    """
    return _fetch_json("/Line/Mode/tube,dlr,overground,elizabeth-line,tram") or []


def _non_bus_line_aliases():
    """Map of lowercased user-typed name -> TfL line id, for tube/DLR/
    Overground/Elizabeth line/tram. A bus route number already IS its own
    line id (e.g. "358"), but the other modes' ids are names rather than
    numbers (e.g. "victoria", "elizabeth", "tram"), and it's friendlier to
    also accept "Victoria", "Victoria line", "Elizabeth Line" etc. than to
    require the user know the exact raw id.

    Fetched once and cached for the lifetime of the process, same pattern
    as get_route_branches.
    """
    global _NON_BUS_LINE_ALIASES_CACHE
    if _NON_BUS_LINE_ALIASES_CACHE is None:
        aliases = {}
        for line in _fetch_non_bus_lines():
            line_id = line["id"]
            name = line["name"].strip().lower()
            aliases[line_id] = line_id
            aliases[name] = line_id
            if name.endswith(" line"):
                # Elizabeth line's own name already ends in "line" - also
                # accept it without the suffix ("elizabeth").
                aliases[name[: -len(" line")]] = line_id
            elif line["modeName"] == "tube":
                # Tube line names are bare ("Victoria", "Central") - also
                # accept the "<name> line" form most people would type.
                aliases[f"{name} line"] = line_id
        _NON_BUS_LINE_ALIASES_CACHE = aliases
    return _NON_BUS_LINE_ALIASES_CACHE


def _non_bus_line_info():
    """Map of TfL line id -> {"name", "mode"}, for tube/DLR/Overground/
    Elizabeth line/tram. Used by line_display_label to tell a bus route
    apart from these - a bus's line id is just its route number, never one
    of these ids.
    """
    global _NON_BUS_LINE_INFO_CACHE
    if _NON_BUS_LINE_INFO_CACHE is None:
        _NON_BUS_LINE_INFO_CACHE = {
            line["id"]: {"name": line["name"], "mode": line["modeName"]}
            for line in _fetch_non_bus_lines()
        }
    return _NON_BUS_LINE_INFO_CACHE


def line_display_label(line_id):
    """Human-readable label for a line id, used wherever a result needs to
    say which service to catch (e.g. "Bus 358", "Victoria line", "DLR",
    "Tram", "Mildmay"). Any id not recognised as tube/DLR/Overground/
    Elizabeth line/tram is assumed to be a bus route number.
    """
    info = _non_bus_line_info().get(line_id)
    if info is None:
        return f"Bus {line_id.upper()}"
    if info["mode"] == "tube":
        return f"{info['name']} line"
    if info["mode"] == "dlr":
        return "DLR"
    if info["mode"] == "tram":
        return "Tram"
    return info["name"]  # Overground lines and Elizabeth line are already full names


def resolve_line_id(route_number):
    """Resolve user-typed input to a TfL line id, without fetching the
    route's stop sequence - just the alias lookup (see
    _non_bus_line_aliases). Used wherever a line id is needed before
    deciding whether to call resolve_route at all (e.g. explore()'s
    already-explored-at-this-radius cache check in app.py) - that check
    must use the same line id resolve_route would end up using, or a
    route typed as an alias (e.g. "Victoria line") would never match its
    own cached results (stored under "victoria").
    """
    typed = route_number.strip().lower()
    return _non_bus_line_aliases().get(typed, typed)


def resolve_route(route_number):
    """Resolve a user-entered route number/name to branches with
    coordinates. Covers bus route numbers (which are already their own
    line id) and tube/DLR/Overground/Elizabeth line/tram, resolved via
    _non_bus_line_aliases - e.g. "358", "victoria", "Victoria line" and
    "DLR" all work.

    Returns a dict: {
        "line_id": ...,
        "branches": [ { direction, branchId, stops: [{id, name, lat, lon}] } ],
    }
    Raises RouteNotFoundError if the route doesn't exist on any covered mode.
    """
    line_id = resolve_line_id(route_number)
    cached_branches = get_route_branches(line_id)
    if not cached_branches:
        raise RouteNotFoundError(
            f"No London bus, tube, DLR, Overground, Elizabeth line or tram route found matching '{route_number}'."
        )

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
