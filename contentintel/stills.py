"""The still picker for storyboards: a picture for a shot comes from the brand's own world or is made for the shot, never from a random stock library, and it must match the shot.

Order for each shot:
1. the PRODUCT's own pictures (the catalogue's images), only when the shot itself is about that product (the shot's words share a product word that is not just the brand's name);
2. the brand's own SITE pictures whose description or file name shares a word with the shot (never the generic front page unless the shot is about the brand itself);
3. a picture GENERATED for the shot from its own description (Pollinations' free image address; clearly labelled generated, never passed off as a product photo).
Openverse, Pixabay, competitors' pictures and the Idea Bank's third-party references are not used here: Storyboard #2 showed unrelated stock (a fishing boy, a gorilla) for shots about something else.
A picture that is not generated and not the product's own must pass the relevance check (relevant()) against the shot, or it is dropped."""
import re
from urllib.parse import quote

STOP = {"with", "that", "this", "then", "from", "into", "over", "show", "shot", "while", "their", "there", "about", "close", "wide", "they", "them", "your", "have", "just", "what", "when", "will", "were", "been",
        "also", "very", "more", "some", "each", "every", "again", "image", "photo", "picture", "front", "page", "brand", "hero", "banner", "logo", "icon", "product", "products", "shop", "store", "home"}
TRUSTED = ("catalogue image", "generated")


def meaningful(text, extra_stop=()):
    return {w for w in re.findall(r"[a-z0-9']+", str(text or "").lower().replace("-", " ").replace("_", " ")) if len(w) >= 4 and w not in STOP and w not in extra_stop and not w.isdigit()}


def shot_words(shot):
    return meaningful(f"{shot.get('visual', '')} {shot.get('spoken', '')}")


def relevant(shot, candidate):
    """True when a picture belongs to its shot: a generated picture and the product's own picture (chosen for the shot by name) always do; any other must share a meaningful word between the shot's own words and the picture's
    description, file name or title."""
    if candidate.get("source") in TRUSTED:
        return True
    text = f"{candidate.get('title', '')} {candidate.get('alt', '')} {str(candidate.get('url', '')).rsplit('/', 1)[-1].rsplit('.', 1)[0]}"
    return bool(shot_words(shot) & meaningful(text))


def generated_still(shot, product_title="", aspect="9:16", seed=1):
    """A picture made for this shot from its own description. Returns a still record whose url draws it when opened."""
    width, height = (720, 1280) if aspect == "9:16" else (1080, 1350) if aspect == "4:5" else (1080, 1080) if aspect == "1:1" else (1280, 720)
    visual = " ".join(str(shot.get("visual") or shot.get("spoken") or "").split())[:240]
    prompt = f"{visual}. {product_title + '. ' if product_title else ''}Natural light, candid photograph, no text, no logos, no watermark."
    return {"url": f"https://image.pollinations.ai/prompt/{quote(prompt)}?width={width}&height={height}&nologo=true&seed={seed}", "title": visual[:120], "license": "generated for this shot", "attribution": "generated image (Pollinations): a stand-in, not a photograph of the product",
            "source": "generated"}


def build_finder(brand, products, aspect_of=lambda board: "9:16", get=None):
    """finder(query, kind, shot=None, board=None) -> candidate pictures for ONE shot, best first, ending with a generated one. `products` is the catalogue's {id: record}."""
    from . import assets_find as AF
    brand_stop = meaningful(f"{getattr(brand, 'name', '')} {getattr(brand, 'slug', '')} {getattr(brand, 'domain', '').split('.')[0] if getattr(brand, 'domain', '') else ''}")
    records = [p for p in (products or {}).values() if isinstance(p, dict) and p.get("handle")]

    def product_for(shot):
        words = shot_words(shot)
        wanted = str(shot.get("product") or "")
        best = None
        for p in records:
            theirs = meaningful(f"{p.get('title', '')} {p['handle']}", brand_stop)
            if theirs & words and (best is None or len(theirs & words) > best[0]):
                best = (len(theirs & words), p)
        if best is None and wanted:
            named = next((p for p in records if p["handle"] == wanted), None)
            if named and meaningful(f"{named.get('title', '')} {named['handle']}", brand_stop) & words:
                best = (1, named)
        return best[1] if best else None

    def finder(query, kind="image", shot=None, board=None):
        shot = shot or {"visual": query, "spoken": "", "product": ""}
        out = []
        product = product_for(shot)
        if product:
            for k, img in enumerate(product.get("images") or []):
                url = img.get("src") or img.get("key")
                if url:
                    out.append({"url": url, "title": f"{product.get('title', '')} (picture {k + 1})", "license": "brand-own", "attribution": getattr(brand, "name", ""), "source": "catalogue image"})
        if get is not None and getattr(brand, "domain", ""):
            try:
                out += [dict(a, source="brand site") for a in AF.site_media(get, brand.domain, query, limit=8, front_page=False)]
            except Exception:                                                            # noqa: BLE001 - the site being down only means no site pictures
                pass
        out.append(generated_still(shot, (product or {}).get("title", ""), aspect_of(board), seed=int(shot.get("n") or 1)))
        return out
    finder.wants_shot = True
    return finder
