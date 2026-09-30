#!/usr/bin/env python3
"""
Command-line lookup tool for the route data produced by fetch_routes.py.

Examples:
    python3 lookup.py line 367
    python3 lookup.py line victoria
    python3 lookup.py line --mode tube
    python3 lookup.py stop "Elmers End"
    python3 lookup.py between "Elmers End" "Beckenham"
"""

import argparse
import json
import os
import sys

DEFAULT_DATA = os.path.join(os.path.dirname(__file__), "data", "routes.json")


def load_data(path):
    if not os.path.exists(path):
        sys.exit(
            f"No data file at {path}. Run 'python3 fetch_routes.py' first to generate it."
        )
    with open(path) as f:
        return json.load(f)


def matches(text, query):
    return query.lower() in text.lower()


def cmd_line(args, data):
    mode_filter = args.mode
    query = args.query

    found = []
    for line_id, line in data.items():
        if mode_filter and line["mode"] != mode_filter:
            continue
        if query and not (matches(line_id, query) or matches(line["name"], query)):
            continue
        found.append(line)

    if not found:
        print("No matching lines.")
        return

    if query and len(found) == 1:
        line = found[0]
        print(f"{line['name']} ({line['mode']}) [{line['id']}]")
        for branch in line["branches"]:
            print(f"  Direction: {branch['direction']} (branch {branch['branchId']})")
            for stop in branch["stops"]:
                print(f"    - {stop['name']}")
        return

    found.sort(key=lambda l: (l["mode"], l["name"]))
    for line in found:
        print(f"{line['mode']:15} {line['name']:10} [{line['id']}]")
    print(f"\n{len(found)} line(s) found.")


def cmd_stop(args, data):
    query = args.query
    results = {}  # line_id -> line dict, for de-dup

    for line_id, line in data.items():
        for branch in line["branches"]:
            for stop in branch["stops"]:
                if matches(stop["name"], query):
                    results[line_id] = line
                    break
            else:
                continue
            break

    if not results:
        print("No lines found serving a stop matching that name.")
        return

    for line in sorted(results.values(), key=lambda l: (l["mode"], l["name"])):
        print(f"{line['mode']:15} {line['name']:10} [{line['id']}]")
    print(f"\n{len(results)} line(s) serve a stop matching '{query}'.")


def cmd_between(args, data):
    a, b = args.stop_a, args.stop_b
    found = []

    for line_id, line in data.items():
        for branch in line["branches"]:
            names = [s["name"] for s in branch["stops"]]
            has_a = any(matches(n, a) for n in names)
            has_b = any(matches(n, b) for n in names)
            if has_a and has_b:
                found.append((line, branch))
                break

    if not found:
        print(f"No direct line found connecting a stop matching '{a}' and '{b}'.")
        return

    for line, branch in found:
        print(f"{line['mode']:15} {line['name']:10} [{line['id']}] - direction: {branch['direction']}")
    print(f"\n{len(found)} line(s) directly connect '{a}' and '{b}'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=DEFAULT_DATA, help="Path to routes.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p_line = sub.add_parser("line", help="Look up a line by id/name, or list lines")
    p_line.add_argument("query", nargs="?", help="Line id or name (e.g. 367, victoria)")
    p_line.add_argument("--mode", help="Filter by mode (bus, tube, dlr, overground, elizabeth-line, tram)")
    p_line.set_defaults(func=cmd_line)

    p_stop = sub.add_parser("stop", help="Find lines serving a stop/station matching a name")
    p_stop.add_argument("query", help="Stop or station name (partial match, case-insensitive)")
    p_stop.set_defaults(func=cmd_stop)

    p_between = sub.add_parser("between", help="Find lines directly connecting two stops")
    p_between.add_argument("stop_a")
    p_between.add_argument("stop_b")
    p_between.set_defaults(func=cmd_between)

    args = parser.parse_args()
    data = load_data(args.data)
    args.func(args, data)


if __name__ == "__main__":
    main()
