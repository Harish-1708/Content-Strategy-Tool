"""Which advertiser page belongs to a brand name: how strongly a page name matches, how distinctive the matched word is, and whether the page only shares a category word.

This is the matching method of last30days-skill (https://github.com/mvanhorn/last30days-skill, MIT licence, copyright 2026 Matt Van Horn; lib/meta_ads.py), re-written here and
used by the main-page finder (contentintel/main_page.py) so that "Chobani" is matched to a page called Chobani and not to any page that merely shares a word with it. The tiers:

  exact      the names are the same once punctuation and spaces are removed
  token      they share a whole word of at least MIN_TOKEN letters
  contained  a whole word of one sits inside the other's compact form (weakest; accepted only when nothing stronger matched)

A word carried by many of the pages in the same result set names a category ("kitchen"), not a company, so rarity is measured against the candidates themselves. A page that shares only
some of a multi-word brand's words (and does not spell the whole name out) is category-only and is never preferred over a page that carries every identifying word.
Nothing here is hardcoded to any brand: it works on whatever names it is given.
"""
import re

MIN_TOKEN = 3
EXACT, TOKEN, CONTAINED, NONE = "exact", "token", "contained", ""
_TOKEN = re.compile(r"[a-z0-9]+")
_STRENGTH_ORDER = {EXACT: 0, TOKEN: 1, CONTAINED: 2, NONE: 3}
# Words that decorate a page title without naming anyone ("Chobani | Facebook", "Chobani - Home"): removed before comparing.
_TITLE_NOISE = {"facebook", "official", "page", "home", "community", "public", "figure", "brand", "store", "shop", "inc", "llc", "ltd", "co", "the"}


def tokens(text):
    return _TOKEN.findall(str(text or "").lower())


def match_tokens(text):
    return {t for t in tokens(text) if len(t) >= MIN_TOKEN and t not in _TITLE_NOISE}


def compact(text):
    return "".join(t for t in tokens(text) if t not in _TITLE_NOISE)


def clean_title(title):
    """A search-result title without the site's own decoration: 'Chobani | Facebook' becomes 'Chobani'."""
    head = re.split(r"\s[|\-–—·]\s|\s\u2022\s", str(title or ""))[0]
    return " ".join(head.split())


def strength(topic, name):
    topic_c, name_c = compact(topic), compact(name)
    if not topic_c or not name_c:
        return NONE
    if topic_c == name_c:
        return EXACT
    a, b = match_tokens(topic), match_tokens(name)
    if not a or not b:
        return NONE
    if a & b:
        return TOKEN
    if any(t in name_c for t in a) or any(t in topic_c for t in b):
        return CONTAINED
    return NONE


def spread(names):
    """How many of these candidate names carry each word."""
    out = {}
    for n in names:
        for t in match_tokens(n):
            out[t] = out.get(t, 0) + 1
    return out


def _matched(topic, name):
    a, b = match_tokens(topic), match_tokens(name)
    shared = a & b
    if shared:
        return shared
    name_c, topic_c = compact(name), compact(topic)
    hit = {t for t in a if t in name_c}
    for t in b:
        if t in topic_c:
            hit |= {x for x in a if t in x or x in t}
    return hit


def rarity(topic, name, counts):
    """Lower is better: the best (rarest) matched word decides."""
    shared = _matched(topic, name)
    return min((counts.get(t, 1) for t in shared), default=0)


def name_covered(topic, name):
    """True when the topic spells out the page's whole name (every word of it, short ones included) and the name accounts for more than one word of the topic, or for all of it."""
    words, topic_words = set(tokens(name)) - _TITLE_NOISE, set(tokens(topic)) - _TITLE_NOISE
    if not words or not topic_words or not words <= topic_words:
        return False
    covered = match_tokens(topic) & words
    return len(covered) >= 2 or (bool(covered) and covered == match_tokens(topic))


def category_only(topic, name):
    """True when the page shares some of the topic's words but is not the brand: a one-word brand is its own identity; a longer name needs every identifying word."""
    matched = _matched(topic, name)
    if not matched:
        return True
    topic_t = match_tokens(topic)
    if len(topic_t) <= 1:
        return False
    if name_covered(topic, name):
        return False
    words, name_c = set(tokens(name)), compact(name)
    return {t for t in topic_t if t in words or t in name_c} != topic_t


def judge(topic, names):
    """For each name: {'name', 'strength', 'rarity', 'category_only'} judged against the whole candidate set (same order)."""
    cleaned = [clean_title(n) for n in names]
    counts = spread(cleaned)
    return [{"name": n, "strength": strength(topic, n), "rarity": rarity(topic, n, counts), "category_only": category_only(topic, n)} for n in cleaned]


def sort_key(verdict):
    """Best first: a stronger tier, then not category-only, then the rarer matched word."""
    return (_STRENGTH_ORDER[verdict["strength"]], verdict["category_only"], verdict["rarity"] or 99)


def explain(verdict):
    if verdict["strength"] == NONE:
        return "its name does not match the brand"
    bits = [f"{verdict['strength']} match"]
    if verdict["category_only"]:
        bits.append("shares only a category word")
    elif verdict["rarity"] == 1:
        bits.append("the matched word is carried by this page alone")
    return ", ".join(bits)
