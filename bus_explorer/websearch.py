"""Last-resort web search for places with no Wikipedia or Wikidata presence.

Some real, well-documented places (e.g. a local park covered by the council's
own website) simply have no Wikipedia article and no Wikidata item at all -
Wikipedia/Wikidata lookups in enrich.py correctly come back empty for these,
even though there is genuine information about the place elsewhere on the
web. This module searches for a real web page about the place and, if one is
found, returns its own text verbatim (never paraphrased or invented) plus a
link to it - same "no invented content" principle as the Wikipedia path.

Requires a Brave Search API key (https://api.search.brave.com) in the
BRAVE_API_KEY environment variable. If it isn't set, search() always returns
None, so the app works exactly as before without it configured.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("BRAVE_API_KEY")
USER_AGENT = "BusExplorer/0.1 (https://github.com/Neil-Holloway/london-transport-routes)"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s):
    """Brave's snippets highlight matched terms with <strong> tags."""
    return _TAG_RE.sub("", s)


def _normalise(s):
    return "".join(ch.lower() for ch in s if ch.isalnum())


def search(name, extra_terms="London"):
    """Returns {snippet, url, title} for the best-matching result, or None.

    Only accepted if the place's name plausibly appears in the result's
    title - a search can easily surface an unrelated page that merely
    mentions the name in passing, and an untitled match is more likely to be
    a coincidence than a page actually about this place.
    """
    if not API_KEY:
        return None

    target = _normalise(name)
    if not target:
        return None

    query = f'"{name}" {extra_terms}'.strip()
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": 5}
    )
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "X-Subscription-Token": API_KEY,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError, urllib.error.HTTPError):
        return None

    for result in data.get("web", {}).get("results", []):
        title = result.get("title", "")
        if target not in _normalise(title):
            continue
        snippet = _strip_tags(result.get("description", "")).strip()
        if snippet:
            return {"snippet": snippet, "url": result.get("url"), "title": title}
    return None
