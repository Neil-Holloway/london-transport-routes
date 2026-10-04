"""OpenRouteService (ORS) client for Park Walks - a real, path-following
walking route, not the nearest-neighbour point-ordering walkplan.py already
does for a list of attractions (see park_walks.py's module docstring for why
a real routing engine was chosen over a dependency-free approximation).

Two modes are used, both via ORS's foot-walking directions endpoint:
  - round_trip_route: a loop of approximately length_m starting and ending
    at the same point - used for "walk around this one park", via ORS's
    options.round_trip (a single start point plus a target length is enough;
    ORS does the route-finding).
  - directions_route: an explicit ordered list of waypoints - used for
    "walk from park A to park B", where the join between two parks is a
    real point-to-point route, not a loop.

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
    summary = feature.get("properties", {}).get("summary", {})
    return {
        "coordinates": coordinates,
        "distance_m": summary.get("distance"),
        "duration_s": summary.get("duration"),
    }


def round_trip_route(lat, lon, length_m, seed=None, points=None):
    """A loop of approximately length_m starting and ending at (lat, lon).

    points controls how many route "shape points" ORS considers when
    building the loop (more points = smoother but slower to compute);
    left to ORS's own default (seemingly a handful) if not given. seed
    lets a specific loop be reproduced (e.g. re-rendering a cached result)
    rather than generating a different random loop of the same length
    each time.
    """
    round_trip = {"length": length_m}
    if points is not None:
        round_trip["points"] = points
    if seed is not None:
        round_trip["seed"] = seed
    payload = {
        "coordinates": [[lon, lat]],
        "options": {"round_trip": round_trip},
    }
    return _post(payload)


def directions_route(waypoints):
    """An explicit ordered route through waypoints: list of (lat, lon)
    tuples, at least 2. Used to join two parks (or more) with a real
    walking path between them, rather than a generated loop.
    """
    if len(waypoints) < 2:
        raise RoutingError("directions_route needs at least 2 waypoints.")
    payload = {"coordinates": [[lon, lat] for lat, lon in waypoints]}
    return _post(payload)
