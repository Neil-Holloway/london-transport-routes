# London Transport Routes

Fetches every London bus, tube, DLR, Overground, Elizabeth line and Tram
route from TfL's Unified API and writes them to a structured JSON file
(line id, name, mode, and full branch-by-branch stop sequences).

## Usage

```bash
python3 fetch_routes.py
```

Optional flags:

- `--modes tube,dlr,overground,elizabeth-line,tram,bus` — restrict to specific modes
- `--out data/routes.json` — output path
- `--app-key YOUR_KEY` — TfL app key (or set `TFL_APP_KEY` env var); not required
  for a one-off run of this size, but recommended for frequent use. Register
  free at https://api-portal.tfl.gov.uk/.
- `--delay 0.1` — seconds between per-line requests

## Output

`data/routes.json` — not checked into git (regenerate with the script).
Keyed by TfL line id, e.g.:

```json
{
  "367": {
    "id": "367",
    "name": "367",
    "mode": "bus",
    "branches": [
      { "direction": "outbound", "branchId": 1, "stops": [...] }
    ]
  }
}
```

## Lookup CLI

Query the generated data without writing any code:

```bash
# Show every stop on a line, by id or name
python3 lookup.py line 367
python3 lookup.py line victoria

# List all lines, optionally filtered by mode
python3 lookup.py line --mode tram

# Find every line serving a stop/station (partial, case-insensitive match)
python3 lookup.py stop "Elmers End"

# Find lines that directly connect two stops
python3 lookup.py between "Elmers End" "Beckenham"
```

## Bus Explorer (Milestone 0.1)

A small Flask app that answers "if I travel on this bus, what interesting
things could I get off and explore?" for a given London bus route. Finds
candidate places from OpenStreetMap within a chosen walking distance of the
route's stops, categorises them, writes a short description (from the
place's linked Wikipedia article where available, or its OSM tags
otherwise), and lets you record a permanent Visited/Favourite/note status
per place — independent of which route you found it from.

Lives in `bus_explorer/`. See `docs/bus-explorer-spec.md` for the full
product specification this implements.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
source .venv/bin/activate
python3 -m bus_explorer.app
```

Then open http://127.0.0.1:5055/, enter a route number (e.g. `54`) and area
(`London`), and choose a maximum walk distance. The first exploration of a
route can take up to a minute (fetching stop coordinates from TfL and
candidate places from OpenStreetMap); results are cached in
`data/bus_explorer.db` (SQLite, not checked into git) for instant reloads
afterwards.

### Notes

- Only London bus routes are supported in this milestone (TfL Unified API).
- Set `TFL_APP_KEY` (free, https://api-portal.tfl.gov.uk/) to raise TfL's
  anonymous rate limit if you hit `429` errors exploring several routes in
  a row.
- No AI drafting yet (spec section 9) — descriptions are pulled directly
  from OpenStreetMap tags and Wikipedia/Wikidata, so every word is
  traceable to a real source.
