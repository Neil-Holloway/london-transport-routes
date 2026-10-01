"""Bus Explorer - Milestone 0.1 Flask app.

Run with:
    source .venv/bin/activate
    python3 -m bus_explorer.app
"""

from flask import Flask, redirect, render_template, request, url_for

from . import db, geocode
from .categorise import CATEGORIES
from .pipeline import explore_route
from .tfl_client import RouteNotFoundError

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/explore", methods=["POST"])
def explore():
    route_number = request.form.get("route_number", "").strip()
    area = request.form.get("area", "").strip()
    max_walk_m = int(request.form.get("max_walk_m", 1000))

    if not route_number:
        return render_template("index.html", error="Please enter a bus route number.")

    if area and "london" not in area.lower():
        return render_template(
            "index.html",
            error="Only London bus routes are supported in this version.",
        )

    line_id = route_number.strip().lower()
    cached_walk_m = db.max_explored_walk_m(line_id)

    if cached_walk_m is not None and cached_walk_m >= max_walk_m:
        # Already explored at this radius (or wider) - skip the slow
        # TfL + OpenStreetMap pipeline and use what's cached.
        return redirect(url_for("route_results", line_id=line_id, walk=max_walk_m))

    try:
        result = explore_route(route_number, max_walk_m)
    except RouteNotFoundError as e:
        return render_template("index.html", error=str(e))
    except RuntimeError as e:
        return render_template(
            "index.html",
            error=f"Could not fetch candidate places right now ({e}). Please try again shortly.",
        )

    return redirect(url_for("route_results", line_id=result["line_id"], walk=max_walk_m))


@app.route("/route/<line_id>")
def route_results(line_id):
    walk_m = request.args.get("walk", type=int)
    view = request.args.get("view", "all")  # highlights | all | unvisited

    results = db.get_route_results(line_id)
    if walk_m:
        results = [r for r in results if r["distance_m"] <= walk_m]

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
    )


@app.route("/attraction/<path:attraction_id>")
def attraction_detail(attraction_id):
    attraction = db.get_attraction(attraction_id)
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
    return render_template("attraction.html", a=attraction, nearby=nearby)


@app.route("/attraction/<path:attraction_id>/visit", methods=["POST"])
def mark_visit(attraction_id):
    visited = request.form.get("visited") == "on"
    date_visited = request.form.get("date_visited") or None
    note = request.form.get("note") or None
    favourite = request.form.get("favourite") == "on"

    db.set_visit(attraction_id, visited, date_visited, note, favourite)

    return_to = request.form.get("return_to") or url_for("attraction_detail", attraction_id=attraction_id)
    return redirect(return_to)


@app.route("/my-visits")
def my_visits():
    category = request.args.get("category") or None
    favourites_only = request.args.get("favourites") == "1"
    line_id = request.args.get("route") or None

    visits = db.get_all_visits(category=category, favourites_only=favourites_only, line_id=line_id)
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
    db.init_db()
    app.run(debug=True, port=5055)


if __name__ == "__main__":
    main()
