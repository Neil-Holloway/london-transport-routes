"""Turns raw OSM tags into displayable text: a short 'why interesting' blurb,
a fuller history, and a source link.

Milestone 0.1 deliberately does not use an LLM to write these — it pulls
directly from OpenStreetMap tags and, where available, the linked Wikipedia
summary. This keeps every word traceable to a real source, per the spec's
'AI should not invent the attractions' principle (here there is no AI at all
yet — this is a placeholder for the future AI drafting step in section 9).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "BusExplorer/0.1 (https://github.com/Georege-Holloway/london-transport-routes)"


def _fetch_wikipedia_summary(wikipedia_tag):
    """wikipedia_tag looks like 'en:Charlton House, London'."""
    if ":" not in wikipedia_tag:
        return None
    lang, title = wikipedia_tag.split(":", 1)
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        extract = data.get("extract")
        page_url = (data.get("content_urls", {}).get("desktop", {}) or {}).get("page")
        if extract:
            return {"extract": extract, "url": page_url or f"https://{lang}.wikipedia.org/wiki/{title}"}
    except (OSError, ValueError):
        return None
    return None


def _wikipedia_tag_from_wikidata(wikidata_id):
    """Resolve a wikidata tag (e.g. 'Q3656999') to an 'en:Title' wikipedia
    tag via its English-language sitelink, for places that only carry a
    wikidata tag in OSM.
    """
    url = (
        "https://www.wikidata.org/w/api.php?action=wbgetentities"
        f"&ids={urllib.parse.quote(wikidata_id)}&props=sitelinks&sitefilter=enwiki&format=json"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        entity = data.get("entities", {}).get(wikidata_id, {})
        title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
        if title:
            return f"en:{title}"
    except (OSError, ValueError):
        return None
    return None


def enrich(tags, category):
    """Returns {why, history, source_url}."""
    name = tags.get("name", "This place")
    wikipedia = tags.get("wikipedia")
    if not wikipedia and tags.get("wikidata"):
        wikipedia = _wikipedia_tag_from_wikidata(tags["wikidata"])
    wiki_summary = _fetch_wikipedia_summary(wikipedia) if wikipedia else None

    if wiki_summary:
        extract = wiki_summary["extract"]
        sentences = extract.split(". ")
        why = sentences[0].strip()
        if not why.endswith("."):
            why += "."
        return {
            "why": why,
            "history": extract,
            "source_url": wiki_summary["url"],
        }

    # Fallback: build something honest from whatever OSM tags exist.
    details = []
    if tags.get("inscription"):
        details.append(f'Inscription: "{tags["inscription"]}"')
    if tags.get("description"):
        details.append(tags["description"])
    if tags.get("start_date"):
        details.append(f"Dates from {tags['start_date']}.")

    why = f"{name} is tagged in OpenStreetMap as {category.lower()}."
    history = " ".join(details) if details else "No further details are available yet from open data sources."

    source_url = None
    if tags.get("wikidata"):
        source_url = f"https://www.wikidata.org/wiki/{tags['wikidata']}"

    return {"why": why, "history": history, "source_url": source_url}
