"""OpenRouteService (ORS) client for Park Walks - a real, path-following
walking route, not the nearest-neighbour point-ordering walkplan.py already
does for a list of attractions (see park_walks.py's module docstring for why
a real routing engine was chosen over a dependency-free approximation).

directions_route - an explicit ordered list of waypoints, via ORS's
foot-walking directions endpoint - is the only mode used. An earlier version
also had a round_trip_route (a loop of approximately a given length from a
single start point, via ORS's options.round_trip) for "walk around this one
park", but that was dropped: ORS's routing graph has no notion that the
start point is a park, only a coordinate, and round_trip-generated loops
measured as just skirting the park on the surrounding streets rather than
actually walking through it. park_walks.py now builds its own explicit
waypoints that force the route through a park's interior instead (see
park_walks._park_loop_waypoints).

Requires ORS_API_KEY (a free account at openrouteservice.org gives 2000
requests/day, 40/minute - ample for this app's traffic). Every request
(even free tier) needs this key, unlike TfL's optional APP_KEY.
"""

import json
import os
import urllib.error
import urllib.request

API_KEY = os.environ.get("ORS_API_KEY")
BASE_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"
USER_AGENT = "BusExplorer/0.1 (https://github.com/Georege-Holloway/london-transport-routes)"


class RoutingError(RuntimeError):
    """Subclasses RuntimeError so app.py can catch routing failures the same
    way it already catches overpass_client's RuntimeError for other
    searches, without needing a separate except clause.
    """


def _post(payload, timeout=25):
    if not API_KEY:
        raise RoutingError("ORS_API_KEY is not configured.")
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RoutingError(f"ORS request failed ({e.code}): {detail}") from e
    except (OSError, ValueError) as e:
        raise RoutingError(f"ORS request failed: {e}") from e

    features = data.get("features") or []
    if not features:
        raise RoutingError("ORS returned no route.")
    feature = features[0]
    coordinates = [(lat, lon) for lon, lat in feature["geometry"]["coordinates"]]
    properties = feature.get("properties", {})
    summary = properties.get("summary", {})

    # ORS breaks a route into one or more "segments" (one per leg between
    # consecutive waypoints), each with its own turn-by-turn "steps" - flatten
    # every segment's steps into a single ordered list, since park_walks.py
    # only ever passes this to the caller as one continuous leg of a walk.
    instructions = []
    for segment in properties.get("segments") or []:
        for step in segment.get("steps") or []:
            instructions.append(
                {
                    "instruction": step.get("instruction"),
                    "distance_m": step.get("distance"),
                    "duration_s": step.get("duration"),
                }
            )

    return {
        "coordinates": coordinates,
        "distance_m": summary.get("distance"),
        "duration_s": summary.get("duration"),
        "instructions": instructions,
    }


def directions_route(waypoints):
    """An explicit ordered route through waypoints: list of (lat, lon)
    tuples, at least 2. Used to join two parks (or more) with a real
    walking path between them, rather than a generated loop.
    """
    if len(waypoints) < 2:
        raise RoutingError("directions_route needs at least 2 waypoints.")
    payload = {"coordinates": [[lon, lat] for lat, lon in waypoints]}
    return _post(payload)
