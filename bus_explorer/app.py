"""Bus Explorer - Milestone 0.1 Flask app.

Run with:
    source .venv/bin/activate
    python3 -m bus_explorer.app
"""

from flask import Flask, g, make_response, redirect, render_template, request, url_for

from . import db, geocode
from .categorise import CATEGORIES
from .categorise_activity import CATEGORIES as ACTIVITY_CATEGORIES
from .pipeline import explore_route
from .tfl_client import RouteNotFoundError
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
            url_for("route_results", line_id=line_id, walk=max_walk_m, categories=selected_categories)
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
        )
    )


@app.route("/route/<line_id>")
def route_results(line_id):
    walk_m = request.args.get("walk", type=int)
    view = request.args.get("view", "all")  # highlights | all | unvisited
    categories = request.args.getlist("categories") or CATEGORIES

    results = db.get_route_results(line_id, g.visitor_name)
    if walk_m:
        results = [r for r in results if r["distance_m"] <= walk_m]
    results = [r for r in results if r["category"] in categories]

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
        results=results,
        view=view,
        walk_m=walk_m,
        categories=categories,
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
        "walk_plan.html", line_id=line_id, plan=plan, totals=totals, maps_url=maps_url
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
        )
    )


@app.route("/activities/<search_key>")
def activity_results(search_key):
    walk_from_m = request.args.get("walk_from", type=int)
    categories = request.args.getlist("categories") or ACTIVITY_CATEGORIES

    results = db.get_journey_results(search_key, g.visitor_name)
    if walk_from_m:
        results = [r for r in results if r["distance_m"] <= walk_from_m]
    results = [r for r in results if r["category"] in categories]

    return render_template(
        "activities_results.html",
        search_key=search_key,
        results=results,
        categories=categories,
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


@app.route("/attraction/<path:attraction_id>/visit", methods=["POST"])
def mark_visit(attraction_id):
    visited = request.form.get("visited") == "on"
    date_visited = request.form.get("date_visited") or None
    note = request.form.get("note") or None
    favourite = request.form.get("favourite") == "on"

    db.set_visit(attraction_id, g.visitor_name, visited, date_visited, note, favourite)

    return_to = request.form.get("return_to") or url_for("attraction_detail", attraction_id=attraction_id)
    return redirect(return_to)


@app.route("/my-visits")
def my_visits():
    category = request.args.get("category") or None
    favourites_only = request.args.get("favourites") == "1"
    line_id = request.args.get("route") or None

    visits = db.get_all_visits(g.visitor_name, category=category, favourites_only=favourites_only, line_id=line_id)
    routes = db.get_routes_explored()
    return render_template(
        "my_visits.html",
        visits=visits,
        categories=CATEGORIES,
        routes=routes,
        selected_category=category,
        favourites_only=favourites_only,
        selected_route=line_id,
    )


def main():
    app.run(debug=True, port=5055)


if __name__ == "__main__":
    main()
