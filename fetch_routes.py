#!/usr/bin/env python3
"""
Fetches all London Underground, DLR, Overground, Elizabeth line, Tram and Bus
routes from TfL's Unified API and writes them to a structured JSON file.

Usage:
    python3 fetch_routes.py [--modes tube,dlr,overground,elizabeth-line,tram,bus]
                             [--out data/routes.json]
                             [--app-key YOUR_KEY]

An app key is optional for this volume of requests but recommended if you'll
run this regularly - register free at https://api-portal.tfl.gov.uk/.
You can also set it via the TFL_APP_KEY environment variable.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.tfl.gov.uk"
DEFAULT_MODES = ["tube", "dlr", "overground", "elizabeth-line", "tram", "bus"]


def fetch_json(path, app_key=None, retries=3):
    url = f"{API_BASE}{path}"
    if app_key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}app_key={urllib.parse.quote(app_key)}"

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2 * attempt)
                continue
            print(f"  ! HTTP {e.code} for {path}", file=sys.stderr)
            return None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(1 * attempt)
                continue
            print(f"  ! Failed {path}: {e}", file=sys.stderr)
            return None


def fetch_all_lines(modes, app_key):
    mode_str = ",".join(modes)
    lines = fetch_json(f"/Line/Mode/{mode_str}", app_key)
    if lines is None:
        raise SystemExit("Could not fetch line list from TfL API")
    return lines


def fetch_route_sequence(line_id, app_key):
    return fetch_json(f"/Line/{line_id}/Route/Sequence/all", app_key)


def summarise_sequence(seq):
    """Reduce the raw TfL response into a compact branch/stop structure."""
    if not seq:
        return []
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES))
    parser.add_argument("--out", default="data/routes.json")
    parser.add_argument("--app-key", default=os.environ.get("TFL_APP_KEY"))
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Seconds to sleep between per-line requests (be polite to the API)",
    )
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    print(f"Fetching line list for modes: {', '.join(modes)}")
    lines = fetch_all_lines(modes, args.app_key)
    print(f"Found {len(lines)} lines")

    result = {}
    for i, line in enumerate(lines, 1):
        line_id = line["id"]
        mode = line["modeName"]
        name = line["name"]
        print(f"[{i}/{len(lines)}] {mode}: {name} ({line_id})")

        seq = fetch_route_sequence(line_id, args.app_key)
        branches = summarise_sequence(seq)

        result[line_id] = {
            "id": line_id,
            "name": name,
            "mode": mode,
            "branches": branches,
        }
        time.sleep(args.delay)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nWrote {len(result)} lines to {args.out}")


if __name__ == "__main__":
    main()
