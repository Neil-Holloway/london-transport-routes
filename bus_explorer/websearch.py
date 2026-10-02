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

# Commercial/aggregator/social domains that come up when a place's name
# happens to match a product, a person, or a travel site lists it as a
# nearby point of interest - none of these are an actual description of the
# place itself, so a title/name match against them is worthless (or, for
# personal social media profiles, a privacy problem - the "match" is someone
# whose name coincides with the place name, not a page about the place).
# Hit in practice: "Engine Block" -> a car-parts shop, "Royal Arsenal Thames
# Path Garden" -> an Agoda hotel listing, a property listing on Zoopla, and
# an individual's personal Facebook page.
_BLOCKED_DOMAINS = (
    "agoda.com", "booking.com", "tripadvisor.", "airbnb.", "expedia.",
    "hotels.com", "flickr.com", "pinterest.", "amazon.", "ebay.",
    "etsy.com", "onlinecarparts.co.uk", "getyourguide.com", "viator.com",
    "zoopla.co.uk", "rightmove.co.uk", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "linkedin.com", "tiktok.com",
)


def _strip_tags(s):
    """Brave's snippets highlight matched terms with <strong> tags."""
    return _TAG_RE.sub("", s)


def _normalise(s):
    return "".join(ch.lower() for ch in s if ch.isalnum())


def search(name, extra_terms="London"):
    """Returns {snippet, url, title} for the best-matching result, or None.

    Two places can share a name (there's a Wellington Park in Somerset as
    well as London), and a search can surface a page that only mentions the
    name in passing rather than being about this place at all. To guard
    against both:
      - the result must come from a domain that isn't a known commercial/
        travel aggregator (see _BLOCKED_DOMAINS)
      - the place's name must appear in the result's title (not just buried
        in the snippet)
      - the title+snippet together must also mention "london" somewhere,
        so a same-named place in a different city doesn't get accepted
    This trades recall for precision deliberately - an honest "no further
    details" beats a confidently-wrong source.
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
        result_url = result.get("url", "")
        if any(domain in result_url for domain in _BLOCKED_DOMAINS):
            continue

        title = result.get("title", "")
        if target not in _normalise(title):
            continue

        snippet = _strip_tags(result.get("description", "")).strip()
        if not snippet:
            continue

        if "london" not in _normalise(title + snippet):
            continue

        return {"snippet": snippet, "url": result_url, "title": title}
    return None
