"""Bus Explorer - Milestone 0.1 Flask app.

Run with:
    source .venv/bin/activate
    python3 -m bus_explorer.app
"""

from flask import Flask, g, make_response, redirect, render_template, request, url_for

from . import db, geocode, tfl_client
from .car_explorer import CAR_CATEGORIES
from .car_explorer import PlaceNotFoundError as CarPlaceNotFoundError
from .car_explorer import _search_key as _car_search_key
from .car_explorer import find_by_car
from .categorise import CATEGORIES
from .categorise_activity import CATEGORIES as ACTIVITY_CATEGORIES
from .park_walks import PlaceNotFoundError as ParkWalkPlaceNotFoundError
from .park_walks import TARGET_DISTANCES_M, find_park_walk
from .park_walks import _search_key as _park_walk_search_key
from .pipeline import explore_route
from .tfl_client import RouteNotFoundError
from .train_explorer import PlaceNotFoundError as TrainPlaceNotFoundError
from .train_explorer import _search_key as _train_search_key
from .train_explorer import find_by_train
from .walkplan import build_plan
from .whatcanido import PlaceNotFoundError, _search_key, find_activities

app = Flask(__name__)
db.init_db()

VISITOR_COOKIE = "visitor_name"
_EXEMPT_ENDPOINTS = {"set_name", "static"}


@app.before_request
def _load_visitor_name():
    g.visitor_name = request.cookies.get(VISITOR_COOKIE)
    if not g.visitor_name and request.endpoint not in _EXEMPT_ENDPOINTS:
        return redirect(url_for("set_name", next=request.full_path))


@app.route("/set-name", methods=["GET", "POST"])
def set_name():
    next_url = request.values.get("next") or url_for("index")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            return render_template("set_name.html", error="Please enter a name.", next=next_url)
        resp = make_response(redirect(next_url))
        resp.set_cookie(VISITOR_COOKIE, name, max_age=60 * 60 * 24 * 365 * 5)
        return resp
    return render_template("set_name.html", next=next_url)


@app.route("/")
def index():
    return render_template("index.html", categories=CATEGORIES)


@app.route("/explore", methods=["POST"])
def explore():
    route_number = request.form.get("route_number", "").strip()
    area = request.form.get("area", "").strip()
    max_walk_m = int(request.form.get("max_walk_m", 1000))
    selected_categories = request.form.getlist("categories") or CATEGORIES
    show_visited = "1" if request.form.get("show_visited") == "1" else "0"
    show_ignored = "1" if request.form.get("show_ignored") == "1" else "0"

    if not route_number:
        return render_template(
            "index.html", categories=CATEGORIES, error="Please enter a bus route number."
        )

    if area and "london" not in area.lower():
        return render_template(
            "index.html",
            categories=CATEGORIES,
            error="Only London bus routes are supported in this version.",
        )

    line_id = route_number.strip().lower()
    cached_walk_m = db.max_explored_walk_m(line_id)

    if cached_walk_m is not None and cached_walk_m >= max_walk_m:
        # Already explored at this radius (or wider) - skip the slow
        # TfL + OpenStreetMap pipeline and use what's cached.
        return redirect(
            url_for(
                "route_results", line_id=line_id, walk=max_walk_m, categories=selected_categories,
                show_visited=show_visited, show_ignored=show_ignored,
            )
        )

    try:
        result = explore_route(route_number, max_walk_m)
    except RouteNotFoundError as e:
        return render_template("index.html", categories=CATEGORIES, error=str(e))
    except RuntimeError as e:
        return render_template(
            "index.html",
            categories=CATEGORIES,
            error=f"Could not fetch candidate places right now ({e}). Please try again shortly.",
        )

    return redirect(
        url_for(
            "route_results",
            line_id=result["line_id"],
            walk=max_walk_m,
            categories=selected_categories,
            show_visited=show_visited,
            show_ignored=show_ignored,
        )
    )


@app.route("/route/<line_id>")
def route_results(line_id):
    walk_m = request.args.get("walk", type=int)
    view = request.args.get("view", "all")  # highlights | all | unvisited
    categories = request.args.getlist("categories") or CATEGORIES
    # Defaults match the behaviour before these were toggleable from the
    # front page: visited items shown, ignored items hidden.
    show_visited = request.args.get("show_visited", "1") == "1"
    show_ignored = request.args.get("show_ignored", "0") == "1"

    results = db.get_route_results(line_id, g.visitor_name, include_ignored=show_ignored)
    if walk_m:
        results = [r for r in results if r["distance_m"] <= walk_m]
    results = [r for r in results if r["category"] in categories]

    if not show_visited:
        results = [r for r in results if not r["visited"]]

    if view == "highlights":
        results = [r for r in results if r["category"] in (
            "Historic buildings", "Museums and galleries", "Memorials and monuments",
            "Music and cultural history", "Famous people/events",
        )]
    elif view == "unvisited":
        results = [r for r in results if not r["visited"]]

    origin = results[0]["nearest_stop_name"] if results else None
    return render_template(
        "route.html",
        line_id=line_id,
        line_label=tfl_client.line_display_label(line_id),
        results=results,
        view=view,
        walk_m=walk_m,
        categories=categories,
        # Passed as "1"/"0" strings (not bools) so the tabs' url_for calls
        # round-trip them back into the same query-string form this route
        # reads with request.args.get(...) == "1".
        show_visited="1" if show_visited else "0",
        show_ignored="1" if show_ignored else "0",
    )


@app.route("/walk-plan", methods=["POST"])
def create_walk_plan():
    line_id = request.form.get("line_id", "")
    attraction_ids = request.form.getlist("attraction_ids")
    if not line_id or not attraction_ids:
        return redirect(url_for("route_results", line_id=line_id))
    return redirect(url_for("walk_plan", line_id=line_id, ids=attraction_ids))


@app.route("/route/<line_id>/walk-plan")
def walk_plan(line_id):
    ids = request.args.getlist("ids")
    route_results = db.get_route_results(line_id, g.visitor_name)
    by_id = {r["id"]: r for r in route_results}
    selected = [by_id[i] for i in ids if i in by_id]

    for stop in selected:
        if not stop.get("address"):
            # Same lazy reverse-geocode as the attraction detail page, so a
            # printed walk pack always has a navigable address per stop.
            address = geocode.reverse_geocode(stop["lat"], stop["lon"])
            if address:
                db.set_address(stop["id"], address)
                stop["address"] = address

    plan, totals = build_plan(selected)

    maps_url = None
    if len(plan) >= 2:
        coords = [f"{s['lat']},{s['lon']}" for s in plan]
        origin, destination = coords[0], coords[-1]
        waypoints = "|".join(coords[1:-1])
        maps_url = (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={origin}&destination={destination}&travelmode=walking"
        )
        if waypoints:
            maps_url += f"&waypoints={waypoints}"
    elif len(plan) == 1:
        maps_url = (
            "https://www.google.com/maps/search/?api=1"
            f"&query={plan[0]['lat']},{plan[0]['lon']}"
        )

    return render_template(
        "walk_plan.html", line_id=line_id, line_label=tfl_client.line_display_label(line_id),
        plan=plan, totals=totals, maps_url=maps_url,
    )


@app.route("/what-can-i-do")
def what_can_i_do():
    return render_template("whatcanido_index.html", categories=ACTIVITY_CATEGORIES)


@app.route("/find-activities", methods=["POST"])
def find_activities_route():
    place_name = request.form.get("place_name", "").strip()
    max_walk_to_stop_m = int(request.form.get("max_walk_to_stop_m", 500))
    max_walk_from_stop_m = int(request.form.get("max_walk_from_stop_m", 1000))
    selected_categories = request.form.getlist("categories") or ACTIVITY_CATEGORIES
    show_visited = "1" if request.form.get("show_visited") == "1" else "0"
    show_ignored = "1" if request.form.get("show_ignored") == "1" else "0"

    if not place_name:
        return render_template(
            "whatcanido_index.html", categories=ACTIVITY_CATEGORIES,
            error="Please enter a starting place.",
        )

    search_key = _search_key(place_name)
    cached = db.max_explored_journey_walk_m(search_key)

    if cached and cached[0] >= max_walk_to_stop_m and cached[1] >= max_walk_from_stop_m:
        return redirect(
            url_for(
                "activity_results", search_key=search_key,
                walk_from=max_walk_from_stop_m, categories=selected_categories,
                show_visited=show_visited, show_ignored=show_ignored,
            )
        )

    try:
        result = find_activities(place_name, max_walk_to_stop_m, max_walk_from_stop_m)
    except PlaceNotFoundError as e:
        return render_template(
            "whatcanido_index.html", categories=ACTIVITY_CATEGORIES, error=str(e)
        )
    except RuntimeError as e:
        return render_template(
            "whatcanido_index.html", categories=ACTIVITY_CATEGORIES,
            error=f"Could not fetch candidate places right now ({e}). Please try again shortly.",
        )

    return redirect(
        url_for(
            "activity_results", search_key=result["search_key"],
            walk_from=max_walk_from_stop_m, categories=selected_categories,
            show_visited=show_visited, show_ignored=show_ignored,
        )
    )


@app.route("/activities/<search_key>")
def activity_results(search_key):
    walk_from_m = request.args.get("walk_from", type=int)
    categories = request.args.getlist("categories") or ACTIVITY_CATEGORIES
    show_visited = request.args.get("show_visited", "1") == "1"
    show_ignored = request.args.get("show_ignored", "0") == "1"

    results = db.get_journey_results(search_key, g.visitor_name, include_ignored=show_ignored)
    if walk_from_m:
        results = [r for r in results if r["distance_m"] <= walk_from_m]
    results = [r for r in results if r["category"] in categories]
    if not show_visited:
        results = [r for r in results if not r["visited"]]
    for r in results:
        r["line_label"] = tfl_client.line_display_label(r["line_id"])

    return render_template(
        "activities_results.html",
        search_key=search_key,
        results=results,
        categories=categories,
    )


@app.route("/car")
def by_car():
    return render_template("car_index.html", categories=CAR_CATEGORIES)


@app.route("/car/search", methods=["POST"])
def car_search():
    place_name = request.form.get("place_name", "").strip()
    radius_m = int(request.form.get("radius_m", 8047))
    selected_categories = request.form.getlist("categories") or CAR_CATEGORIES
    show_visited = "1" if request.form.get("show_visited") == "1" else "0"
    show_ignored = "1" if request.form.get("show_ignored") == "1" else "0"

    if not place_name:
        return render_template(
            "car_index.html", categories=CAR_CATEGORIES, error="Please enter a starting place."
        )

    search_key = _car_search_key(place_name)
    cached_radius_m = db.max_explored_car_radius_m(search_key)

    if cached_radius_m is not None and cached_radius_m >= radius_m:
        # Already explored at this radius (or wider) - skip the slow
        # OpenStreetMap pipeline and use what's cached. The cached data was
        # always fetched/scored across the full CAR_CATEGORIES set (category
        # selection only narrows the Overpass query, not per-user), so this
        # is safe regardless of which categories are selected this time.
        return redirect(
            url_for(
                "car_results", search_key=search_key, radius=radius_m,
                categories=selected_categories, show_visited=show_visited, show_ignored=show_ignored,
            )
        )

    try:
        result = find_by_car(place_name, radius_m)
    except CarPlaceNotFoundError as e:
        return render_template("car_index.html", categories=CAR_CATEGORIES, error=str(e))
    except RuntimeError as e:
        return render_template(
            "car_index.html",
            categories=CAR_CATEGORIES,
            error=f"Could not fetch candidate places right now ({e}). Please try again shortly.",
        )

    return redirect(
        url_for(
            "car_results", search_key=result["search_key"], radius=radius_m,
            categories=selected_categories, show_visited=show_visited, show_ignored=show_ignored,
        )
    )


@app.route("/car/<search_key>")
def car_results(search_key):
    radius_m = request.args.get("radius", type=int)
    categories = request.args.getlist("categories") or CAR_CATEGORIES
    show_visited = request.args.get("show_visited", "1") == "1"
    show_ignored = request.args.get("show_ignored", "0") == "1"

    results = db.get_car_results(search_key, g.visitor_name, include_ignored=show_ignored)
    if radius_m:
        results = [r for r in results if r["distance_m"] <= radius_m]
    results = [r for r in results if r["category"] in categories]
    if not show_visited:
        results = [r for r in results if not r["visited"]]

    return render_template(
        "car_results.html",
        search_key=search_key,
        results=results,
        categories=categories,
        show_visited="1" if show_visited else "0",
        show_ignored="1" if show_ignored else "0",
    )


@app.route("/train")
def by_train():
    return render_template("train_index.html", categories=CATEGORIES)


@app.route("/train/search", methods=["POST"])
def train_search():
    place_name = request.form.get("place_name", "").strip()
    max_walk_to_station_m = int(request.form.get("max_walk_to_station_m", 1500))
    max_stops = int(request.form.get("max_stops", 5))
    selected_categories = request.form.getlist("categories") or CATEGORIES
    show_visited = "1" if request.form.get("show_visited") == "1" else "0"
    show_ignored = "1" if request.form.get("show_ignored") == "1" else "0"

    if not place_name:
        return render_template(
            "train_index.html", categories=CATEGORIES, error="Please enter a starting place."
        )

    search_key = _train_search_key(place_name)
    cached = db.max_explored_train_stops(search_key)

    if cached and cached[0] >= max_walk_to_station_m and cached[1] >= max_stops:
        # Already explored at this walk distance/stop count (or wider) -
        # skip the slow TfL + OpenStreetMap pipeline and use what's cached.
        return redirect(
            url_for(
                "train_results", search_key=search_key, walk=max_walk_to_station_m, max_stops=max_stops,
                categories=selected_categories, show_visited=show_visited, show_ignored=show_ignored,
            )
        )

    try:
        result = find_by_train(place_name, max_walk_to_station_m, max_stops)
    except TrainPlaceNotFoundError as e:
        return render_template("train_index.html", categories=CATEGORIES, error=str(e))
    except RuntimeError as e:
        return render_template(
            "train_index.html",
            categories=CATEGORIES,
            error=f"Could not fetch candidate places right now ({e}). Please try again shortly.",
        )

    return redirect(
        url_for(
            "train_results", search_key=result["search_key"], walk=max_walk_to_station_m, max_stops=max_stops,
            categories=selected_categories, show_visited=show_visited, show_ignored=show_ignored,
        )
    )


@app.route("/train/<search_key>")
def train_results(search_key):
    walk_m = request.args.get("walk", type=int)
    categories = request.args.getlist("categories") or CATEGORIES
    show_visited = request.args.get("show_visited", "1") == "1"
    show_ignored = request.args.get("show_ignored", "0") == "1"

    results = db.get_train_results(search_key, g.visitor_name, include_ignored=show_ignored)
    if walk_m:
        results = [r for r in results if r["distance_m"] <= walk_m]
    results = [r for r in results if r["category"] in categories]
    if not show_visited:
        results = [r for r in results if not r["visited"]]
    for r in results:
        r["line_label"] = tfl_client.line_display_label(r["line_id"])

    return render_template(
        "train_results.html",
        search_key=search_key,
        results=results,
        categories=categories,
        show_visited="1" if show_visited else "0",
        show_ignored="1" if show_ignored else "0",
    )


@app.route("/park-walks")
def park_walks_index():
    return render_template("park_walks_index.html", target_distances=TARGET_DISTANCES_M)


@app.route("/park-walks/search", methods=["POST"])
def park_walks_search():
    place_name = request.form.get("place_name", "").strip()
    target_distance_m = int(request.form.get("target_distance_m", TARGET_DISTANCES_M[0]))
    force = request.form.get("force") == "1"

    if not place_name:
        return render_template(
            "park_walks_index.html", target_distances=TARGET_DISTANCES_M,
            error="Please enter a starting place.",
        )

    search_key = _park_walk_search_key(place_name)
    cached = None if force else db.get_park_walk(search_key, target_distance_m)

    if cached is None:
        try:
            find_park_walk(place_name, target_distance_m)
        except ParkWalkPlaceNotFoundError as e:
            return render_template(
                "park_walks_index.html", target_distances=TARGET_DISTANCES_M, error=str(e)
            )
        except RuntimeError as e:
            return render_template(
                "park_walks_index.html",
                target_distances=TARGET_DISTANCES_M,
                error=f"Could not generate a route right now ({e}). Please try again shortly.",
            )

    return redirect(url_for("park_walks_results", search_key=search_key, distance=target_distance_m))


@app.route("/park-walks/<search_key>")
def park_walks_results(search_key):
    target_distance_m = request.args.get("distance", type=int) or TARGET_DISTANCES_M[0]
    walk = db.get_park_walk(search_key, target_distance_m)
    if not walk:
        return redirect(url_for("park_walks_index"))

    return render_template(
        "park_walks_results.html",
        walk=walk,
        target_distances=TARGET_DISTANCES_M,
    )


@app.route("/attraction/<path:attraction_id>")
def attraction_detail(attraction_id):
    attraction = db.get_attraction(attraction_id, g.visitor_name)
    if not attraction:
        return "Attraction not found", 404

    if not attraction.get("address"):
        # No addr:* tags on the OSM element itself - reverse-geocode once
        # and cache the result, rather than looking this up for every
        # candidate during route exploration (Nominatim rate-limits to
        # 1 request/second).
        address = geocode.reverse_geocode(attraction["lat"], attraction["lon"])
        if address:
            db.set_address(attraction_id, address)
            attraction["address"] = address

    nearby = db.get_nearby(attraction_id)
    is_activity = attraction["category"] in ACTIVITY_CATEGORIES
    return render_template("attraction.html", a=attraction, nearby=nearby, is_activity=is_activity)


@app.route("/attraction/<path:attraction_id>/toggle-visited", methods=["POST"])
def toggle_visited(attraction_id):
    """Quick 'mark visited' / 'undo' action from a results-page card - no
    date is recorded (dropped per the request to simplify this to a single
    click), unlike the old detail-page form this replaces.
    """
    visited = request.form.get("visited") == "1"
    db.set_visited(attraction_id, g.visitor_name, visited)

    return_to = request.form.get("return_to") or url_for("attraction_detail", attraction_id=attraction_id)
    return redirect(return_to)


@app.route("/attraction/<path:attraction_id>/ignore", methods=["POST"])
def toggle_ignored(attraction_id):
    """Hides (or restores) this attraction from the current visitor's
    results listings (see db.set_ignored and the ignored filter in
    get_route_results / get_journey_results / get_all_visits). Toggles
    both ways, same pattern as toggle_visited above - un-ignoring only
    makes sense while the "show ignored" display option is on, since
    that's the only way an ignored item is visible to click the button on.
    """
    ignored = request.form.get("ignored") == "1"
    db.set_ignored(attraction_id, g.visitor_name, ignored)

    return_to = request.form.get("return_to") or url_for("index")
    return redirect(return_to)


@app.route("/attraction/<path:attraction_id>/note", methods=["POST"])
def save_note(attraction_id):
    note = request.form.get("note") or None
    favourite = request.form.get("favourite") == "on"

    db.set_note_favourite(attraction_id, g.visitor_name, note, favourite)

    return_to = request.form.get("return_to") or url_for("attraction_detail", attraction_id=attraction_id)
    return redirect(return_to)


@app.route("/my-visits")
def my_visits():
    category = request.args.get("category") or None
    favourites_only = request.args.get("favourites") == "1"
    line_id = request.args.get("route") or None

    visits = db.get_all_visits(g.visitor_name, category=category, favourites_only=favourites_only, line_id=line_id)
    routes = db.get_routes_explored()
    route_labels = {r: tfl_client.line_display_label(r) for r in routes}
    return render_template(
        "my_visits.html",
        visits=visits,
        categories=CATEGORIES,
        routes=routes,
        route_labels=route_labels,
        selected_category=category,
        favourites_only=favourites_only,
        selected_route=line_id,
    )


def main():
    app.run(debug=True, port=5055)


if __name__ == "__main__":
    main()
