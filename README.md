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
