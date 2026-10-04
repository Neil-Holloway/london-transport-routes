"""OpenRouteService (ORS) client for Park Walks - a real, path-following
walking route, not the nearest-neighbour point-ordering walkplan.py already
does for a list of attractions (see park_walks.py's module docstring for why
a real routing engine was chosen over a dependency-free approximation).

round_trip_route generates a loop of approximately length_m starting and
ending at the same point, via ORS's foot-walking directions endpoint's
options.round_trip (a single start point plus a target length is enough;
ORS does the route-finding). park_walks.py starts this from a park's
centroid rather than its boundary, and also passes avoid_polygon - see
_park_loop_route/_avoid_polygon_outside_park there for why both are needed:
round_trip's own length-targeting has no concept of staying inside an area
at all, so a centroid start only guarantees the first and last stretch use
an in-park path - the rest of the loop is free to wander onto surrounding
streets to make up the requested distance. avoid_polygon makes that a hard
constraint instead of a nudge, by marking everything outside the park as
off-limits to the router.

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


def round_trip_route(lat, lon, length_m, seed=None, points=None, avoid_polygon=None):
    """A loop of approximately length_m starting and ending at (lat, lon).

    points controls how many route "shape points" ORS considers when
    building the loop (more points = smoother but slower to compute);
    left to ORS's own default (seemingly a handful) if not given. seed
    lets a specific loop be reproduced (e.g. re-rendering a cached result)
    rather than generating a different random loop of the same length
    each time.

    avoid_polygon, if given, is a GeoJSON Polygon/MultiPolygon geometry
    (not a Feature) passed as ORS's options.avoid_polygons - any area the
    router may not route through at all, as a hard constraint rather than
    a mere starting-point bias (see park_walks._avoid_polygon_outside_park).
    If the only roads/paths that could satisfy length_m all fall inside the
    avoided area, ORS raises a RoutingError (via _post's "no route" check)
    rather than silently routing through it.
    """
    round_trip = {"length": length_m}
    if points is not None:
        round_trip["points"] = points
    if seed is not None:
        round_trip["seed"] = seed
    options = {"round_trip": round_trip}
    if avoid_polygon is not None:
        options["avoid_polygons"] = avoid_polygon
    payload = {
        "coordinates": [[lon, lat]],
        "options": options,
    }
    return _post(payload)
