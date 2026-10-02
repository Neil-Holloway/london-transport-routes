"""One-off: for attractions whose history is still the generic "No further
details..." placeholder (Wikipedia and Wikidata had nothing), try the new
web-search fallback and update why/history/source_url if a real match is
found.

Run once after setting BRAVE_API_KEY (and DATABASE_URL), then delete:
    source .venv/bin/activate
    export DATABASE_URL='...'
    export BRAVE_API_KEY='...'
    python3 backfill_websearch.py
"""
from bus_explorer import db, websearch
from bus_explorer.enrich import NO_FURTHER_DETAILS


def main():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM attractions WHERE history = %s",
                (NO_FURTHER_DETAILS,),
            )
            rows = cur.fetchall()

    print(f"Found {len(rows)} attractions with no further details.")
    confident_count = 0
    hedged_count = 0
    for row in rows:
        result = websearch.search(row["name"])
        if not result:
            continue
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                if result["confident"]:
                    why = f"{row['name']} — {result['snippet']}"
                    cur.execute(
                        "UPDATE attractions SET why = %s, history = NULL, source_url = %s WHERE id = %s",
                        (why, result["url"], row["id"]),
                    )
                    confident_count += 1
                    print(f"Updated (confident): {row['name']} -> {result['url']}")
                else:
                    history = (
                        "Possibly related information found online, not verified as "
                        f"being about this exact place: \"{result['snippet']}\""
                    )
                    cur.execute(
                        "UPDATE attractions SET history = %s, source_url = %s WHERE id = %s",
                        (history, result["url"], row["id"]),
                    )
                    hedged_count += 1
                    print(f"Updated (possibly related): {row['name']} -> {result['url']}")

    total_updated = confident_count + hedged_count
    print(
        f"Updated {total_updated} of {len(rows)} "
        f"({confident_count} confident, {hedged_count} possibly related), "
        f"skipped {len(rows) - total_updated}."
    )


if __name__ == "__main__":
    main()
