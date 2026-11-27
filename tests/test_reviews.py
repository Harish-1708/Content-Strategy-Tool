"""Customer reviews kept word for word, and the brief's quotes checked against them (Update 73 v6)."""
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from contentintel import reviews as R
from contentintel import reviews_cli
from contentintel.store import Store

NOW = "2026-11-26T08:00:00Z"
EXPORT = ('\ufefftitle,body,rating,review_date,source,curated,reviewer_name,reviewer_email,product_id,product_handle,reply,picture_urls,ip_address,location\n'
          'Love it,"So soft and the pockets are huge, I wear it every single day.",5,2026-09-01 10:00:00 UTC,web,ok,Jess P.,jess@example.com,1,dude-robe,,,10.0.0.1,US\n'
          'Meh,"It is fine.",3,2026-08-15,web,ok,Sam T.,sam@example.com,1,dude-robe,,,10.0.0.2,US\n'
          'Great,"Warm without being heavy: exactly what I wanted for the cold mornings.",5,2026-10-02,web,ok,Alex R.,alex@example.com,2,dude-wipes,,,10.0.0.3,CA\n'
          'Empty,,5,2026-10-03,web,ok,No Body,nb@example.com,2,dude-wipes,,,10.0.0.4,CA\n')
WIDGET = ('<html><body><div class="jdgm-rev-widg"><div class="jdgm-rev jdgm-divider-top" data-review-id="1"><span class="jdgm-rev__rating" data-score="5"></span>'
          '<span class="jdgm-rev__timestamp" data-content="2026-09-20 12:00:00 UTC">a month ago</span><span class="jdgm-rev__author">Pat K.</span>'
          '<b class="jdgm-rev__title">Best robe</b><div class="jdgm-rev__body"><p>It&#39;s the softest robe I own, and it dries fast.</p></div></div>'
          '<div class="jdgm-rev" data-review-id="2"><span class="jdgm-rev__rating" data-score="4"></span><span class="jdgm-rev__author">Lee</span>'
          '<div class="jdgm-rev__body"><p>Good value.</p><p>Would buy again.</p></div></div></div></body></html>')


def reviews_of(*rows):
    return [R.make_review(b, rating=5, reviewer=n, date=d, handle=h, source="judge.me export") for b, n, d, h in rows]


def test_the_export_is_read_by_column_name_with_no_email_or_ip_kept_and_empty_rows_counted():
    reviews, skipped = R.parse_export(EXPORT)
    assert skipped == 1 and len(reviews) == 3 and {r["rating"] for r in reviews} == {5, 3}
    first = next(r for r in reviews if r["reviewer"] == "Jess P.")
    assert first["body"] == "So soft and the pockets are huge, I wear it every single day." and first["date"] == "2026-09-01" and first["handle"] == "dude-robe" and first["id"].startswith("RV-")
    blob = json.dumps(reviews)
    assert "example.com" not in blob and "10.0.0." not in blob and "reviewer_email" not in blob and "ip_address" not in blob                            # never read, never kept
    shuffled = "body,reviewer_name,stars,date\n\"Nice and warm all winter long.\",Kim,5,2026-01-02\n"
    other, _ = R.parse_export(shuffled)
    assert other[0]["body"] == "Nice and warm all winter long." and other[0]["reviewer"] == "Kim" and other[0]["rating"] == 5                              # columns in any order, common names accepted
    assert R.parse_export("") == ([], 0)
    assert R.review_id("A", "2026-01-01", "same words") == R.review_id("A", "2026-01-01T10:00", "same   words") != R.review_id("B", "2026-01-01", "same words")


def test_the_widget_on_a_product_page_gives_the_first_reviews_and_a_page_that_will_not_parse_gives_none():
    got = R.parse_widget(WIDGET, handle="dude-robe", url="https://shop.example/products/dude-robe")
    assert [r["reviewer"] for r in got] == ["Pat K.", "Lee"] and got[0]["body"] == "It's the softest robe I own, and it dries fast." and got[0]["rating"] == 5 and got[0]["date"] == "2026-09-20"
    assert got[0]["title"] == "Best robe" and got[0]["url"].endswith("/dude-robe") and "widget" in got[0]["source"] and got[1]["rating"] == 4 and "Good value." in got[1]["body"] and "Would buy again." in got[1]["body"]
    assert R.parse_widget("<html><body>no reviews here</body></html>") == [] and R.parse_widget("") == [] and R.parse_widget(None) == []
    both = WIDGET.replace('<div class="jdgm-rev-widg">', '<div class="jdgm-rev jdgm-rev-widg">')                                                                    # a wrapper that carries both class names is still only the wrapper
    assert [r["reviewer"] for r in R.parse_widget(both)] == ["Pat K.", "Lee"]


def test_reviews_are_stored_once_newest_first_with_no_private_data(tmp_path):
    store = Store(tmp_path / "data")
    first, _ = R.parse_export(EXPORT)
    assert R.merge(store, "duderobe", first, "judge.me export", NOW) == (3, 3)
    assert R.merge(store, "duderobe", first, "judge.me export", NOW) == (0, 3)                                                                      # the same review is never added twice
    more = R.parse_widget(WIDGET, handle="dude-robe")
    assert R.merge(store, "duderobe", more, "widget", NOW) == (2, 5)
    data = R.load(store, "duderobe")
    assert data["count"] == 5 and [r["date"] for r in data["reviews"]] == sorted((r["date"] for r in data["reviews"]), reverse=True) and data["updated"] == NOW and len(data["sources"]) >= 1
    assert R.load(store, "nobody") == {"reviews": []}


def test_the_writer_is_offered_only_five_star_reviews_of_a_quotable_length_the_products_first_and_newest_first():
    rows = [("Soft and warm all winter long, I love it so much.", "A", "2026-09-01", "dude-robe"), ("Great", "B", "2026-10-01", "dude-robe"), ("Warm without being heavy: exactly what I wanted.", "C", "2026-10-05", "other"),
            ("x" * 400, "D", "2026-10-06", "dude-robe"), ("Pockets big enough for a phone and a book, every day.", "E", "2026-10-02", "dude-robe")]
    reviews = reviews_of(*rows) + [R.make_review("This one is only three stars but long enough.", rating=3, reviewer="F", date="2026-10-09", handle="dude-robe")]
    picked = R.pick_for_prompt(reviews, ["dude-robe"], limit=8)
    assert [r["reviewer"] for r in picked] == ["E", "A", "C"]                                                                                         # too short, too long and not five star are left out; the product's own first, newest first
    assert len(R.pick_for_prompt(reviews, ["dude-robe"], limit=2)) == 2 and R.prompt_block([]) == ""
    block = R.prompt_block(picked)
    assert block.startswith("Customer reviews you may quote.") and f"- {picked[0]['id']}: \"Pockets big enough for a phone and a book, every day.\" (E, 2026-10-02, 5 stars)" in block and "never edited" in block and "review_id" in block


def test_a_quote_is_word_for_word_from_one_review_and_anything_edited_combined_or_invented_is_an_error():
    stored = reviews_of(("So soft and the pockets are huge, I wear it every single day.", "Jess", "2026-09-01", "r"), ("Warm without being heavy: exactly what I wanted.", "Alex", "2026-10-02", "r"))
    jess = stored[0]

    def brief(**c):
        return {"concepts": [{"name": "A", "hook": "", "alt_hook": "", "script": "", "visual_direction": "", "cta": "", **c}]}
    assert R.check_quotes(brief(script='Show it. "So soft and the pockets are huge." Then smile.'), stored) == []                                          # a sentence-part, exactly as written
    assert R.check_quotes(brief(script="He said \u201cSo soft and the pockets are huge, I wear it every single day.\u201d"), stored) == []                 # curly quotes are the same quote
    polished = R.check_quotes(brief(script='"So soft and the pockets are enormous, I wear it every day."'), stored)
    assert [f["rule"] for f in polished] == ["quote_not_verbatim"] and polished[0]["severity"] == "error" and polished[0]["where"] == "concept 1 (A) script"
    curly_invented = R.check_quotes(brief(script="He said \u201cSo soft and the pockets are enormous, I wear it every day.\u201d"), stored)
    assert [f["rule"] for f in curly_invented] == ["quote_not_verbatim"]                                                                               # curly quotation marks do not hide an edited quote
    joined = R.check_quotes(brief(hook='"So soft and the pockets are huge, exactly what I wanted."'), stored)
    assert [f["rule"] for f in joined] == ["quote_not_verbatim"]                                                                                      # two reviews never join
    across = R.check_quotes(brief(hook='"I wear it every single day. Warm without being heavy"'), stored)
    assert [f["rule"] for f in across] == ["quote_not_verbatim"]                                                                                      # the end of one review and the start of the next, side by side, is still not one review's words
    assert R.check_quotes(brief(script='"It changed my life completely, honestly."'), [])[0]["rule"] == "quote_unverifiable"                                  # no review stored: no quote can be verified
    assert R.check_quotes(brief(script='He said "so soft" and nodded'), stored) == []                                                                  # under four words: not a customer quote
    assert R.check_quotes(brief(script='He said "absolutely loved it" and nodded'), stored) == []                                                      # three words, long enough in letters: still not treated as a customer quote
    assert [f["rule"] for f in R.check_quotes(brief(script='He said "absolutely loved it at last" and nodded'), stored)] == ["quote_not_verbatim"]      # four words is where a quoted passage starts to count
    ok = R.check_quotes(brief(quote={"review_id": jess["id"], "text": "the pockets are huge, I wear it every single day"}), stored)
    assert ok == []
    assert R.check_quotes(brief(quote={"review_id": jess["id"], "text": "the pockets are huge, I wear it daily"}), stored)[0]["rule"] == "quote_not_verbatim"
    unknown = R.check_quotes(brief(quote={"review_id": "RV-deadbeef", "text": "anything at all here"}), stored)[0]
    assert unknown["rule"] == "quote_unknown_review" and unknown["severity"] == "error"
    assert R.check_quotes(brief(quote={"review_id": stored[1]["id"], "text": "the pockets are huge, I wear it every single day"}), stored)[0]["rule"] == "quote_not_verbatim"          # right words, wrong review
    assert R.attribution(jess) == f"Jess, 2026-09-01 (judge.me export, 5 stars, {jess['id']})" and R.find_quote("tiny", stored) is None


class Args(SimpleNamespace):
    pass


def test_the_reviews_command_imports_collects_and_reports_what_it_did(tmp_path, capsys):
    store = Store(tmp_path / "data")
    export = tmp_path / "judgeme.csv"
    export.write_text(EXPORT, encoding="utf-8")
    args = Args(action="import", brand="duderobe", file=str(export), brands_dir="brands", max_products=60, pause=0.0)
    assert reviews_cli.run_reviews(args, store, NOW) == 0
    assert "read 3 review(s) from" in capsys.readouterr().out and R.load(store, "duderobe")["count"] == 3
    args.file = str(tmp_path / "missing.csv")
    assert reviews_cli.run_reviews(args, store, NOW) == 1 and "no export file at" in capsys.readouterr().out
    args.action, args.file = "status", ""
    reviews_cli.run_reviews(args, store, NOW)
    out = capsys.readouterr().out
    assert "duderobe: 3 review(s) stored, 2 of them five star, newest 2026-10-02" in out and "judge.me export 3" in out
    reviews_cli.run_reviews(Args(action="status", brand="defi_snacks", file="", brands_dir="brands", max_products=60, pause=0.0), store, NOW)
    assert "a brief cannot quote a customer for this brand" in capsys.readouterr().out
    store.promote_latest("duderobe", {"brand": "duderobe", "products": {"1": {"handle": "dude-robe"}, "2": {"handle": "dude-wipes"}}})


def test_the_reviews_workflow_runs_weekly_by_itself_collects_by_default_and_saves_with_the_helper():
    wf = yaml.safe_load(Path(".github/workflows/reviews.yml").read_text(encoding="utf-8"))
    assert wf[True]["schedule"] == [{"cron": "30 6 * * 0"}] and wf["jobs"]["reviews"]["timeout-minutes"] == 200 and wf[True]["workflow_dispatch"]["inputs"]["force"]["default"] is False and wf[True]["workflow_dispatch"]["inputs"]["action"]["options"] == ["collect", "import", "status"] and wf[True]["workflow_dispatch"]["inputs"]["action"]["default"] == "collect"
    text = Path(".github/workflows/reviews.yml").read_text(encoding="utf-8")
    assert "add_each data/*/extras/reviews.json" in text and "bash .github/scripts/push-with-retry.sh" in text and "--file" in text and "OPTIONAL: the Judge.me export file" in text and 'args+=(--force)' in text


# ---- the automatic, complete collection --------------------------------------------------------------------------------------------------------------------------------------------------------

PRODUCT_PAGE = ('<html><head><script>Shopify.shop = "dude.myshopify.com";</script></head><body><div class="jdgm-widget jdgm-review-widget" data-id="{pid}"></div>{reviews}</body></html>')


def block(r):
    return (f'<div class="jdgm-rev" data-review-id="{r["n"]}"><span class="jdgm-rev__rating" data-score="5"></span><span class="jdgm-rev__timestamp" data-content="{r["date"]} 10:00:00 UTC"></span>'
            f'<span class="jdgm-rev__author">{r["who"]}</span><div class="jdgm-rev__body"><p>{r["body"]}</p></div></div>')


def make_reviews(prefix, count, start="2026-01"):
    return [{"n": f"{prefix}{k}", "date": f"{start}-{(k % 27) + 1:02d}", "who": f"{prefix} person {k}", "body": f"Review number {k} of {prefix}: the robe is soft, warm and big enough for everyone."} for k in range(count)]


class FakeJudgeMe:
    """The brand's product pages and Judge.me's page address, serving pages of reviews newest first."""

    def __init__(self, products, per_page=10):
        self.products, self.asked, self.per_page = products, [], per_page                                                                           # {handle: (product id, [review dicts, newest first])}
        self.refuse = set()

    def get(self, url):
        self.asked.append(url)
        if "judge.me/reviews/reviews_for_widget" in url:
            q = dict(x.split("=") for x in url.split("?")[1].split("&"))
            pid = q["product_id"]
            if pid in self.refuse:
                return 403, ""
            handle = next(h for h, (i, _r) in self.products.items() if i == pid)
            rows = self.products[handle][1]
            page, per = int(q["page"]), int(q["per_page"])
            chunk = rows[(page - 1) * per: page * per]
            return 200, json.dumps({"html": "".join(block(r) for r in chunk)})
        handle = url.rsplit("/products/", 1)[1]
        if handle not in self.products:
            return 404, ""
        pid, rows = self.products[handle]
        return 200, PRODUCT_PAGE.format(pid=pid, reviews="".join(block(r) for r in rows[:5]))


def collect_args(**over):
    base = dict(action="collect", brand="duderobe", file="", brands_dir="brands", max_products=0, pause=0.0, per_page=10, time_budget=150, topup_days=28, force=False)
    base.update(over)
    return Args(**base)


def store_with_products(tmp_path, handles):
    store = Store(tmp_path / "data")
    store.promote_latest("duderobe", {"brand": "duderobe", "products": {str(i): {"handle": h} for i, h in enumerate(handles)}})
    return store


def test_the_first_collection_reads_every_page_of_every_products_reviews_and_saves_progress(tmp_path, capsys):
    robe, wipes = make_reviews("robe", 25), make_reviews("wipes", 7)
    server = FakeJudgeMe({"dude-robe": ("111", robe), "dude-wipes": ("222", wipes)})
    store = store_with_products(tmp_path, ["dude-robe", "dude-wipes"])
    slept = []
    assert reviews_cli.run_reviews(collect_args(pause=1.0), store, NOW, get=server.get, sleep=slept.append) == 0
    out = capsys.readouterr().out
    data = R.load(store, "duderobe")
    assert data["count"] == 32 and len({r["id"] for r in data["reviews"]}) == 32                                                                       # the whole history, each review once (the first page was also drawn into the product page)
    assert "read 2 of 2 product(s) (4 page(s) of reviews), found 32 review(s), 32 new; the store holds 32; 2 of 2 product(s) read in full" in out
    pages_asked = [u.split("page=")[1].split("&")[0] for u in server.asked if "reviews_for_widget" in u and "product_id=111" in u]
    assert pages_asked == ["1", "2", "3", "4"] and all("per_page=10" in u for u in server.asked if "judge.me" in u)                                    # pages 1-3 hold reviews, page 4 is empty: the end
    assert data["collect"]["dude-robe"]["complete"] is True and data["collect"]["dude-robe"]["pages"] == 3 and data["collect"]["dude-robe"]["topup_at"] == NOW and set(slept) == {1.0}
    assert all(r["handle"] in ("dude-robe", "dude-wipes") and r["source"].startswith("judge.me widget") for r in data["reviews"])
    again = len(server.asked)
    assert reviews_cli.run_reviews(collect_args(), store, NOW, get=server.get, sleep=lambda s: None) == 0
    assert "up to date: all 2 product(s) were read in full" in capsys.readouterr().out and len(server.asked) == again                                  # nothing is asked again until a top-up is due


def test_a_top_up_reads_only_the_newest_pages_until_it_meets_what_is_stored_and_only_when_it_is_due(tmp_path, capsys):
    robe = make_reviews("robe", 25)
    server = FakeJudgeMe({"dude-robe": ("111", robe)})
    store = store_with_products(tmp_path, ["dude-robe"])
    reviews_cli.run_reviews(collect_args(), store, "2026-10-01T00:00:00Z", get=server.get, sleep=lambda s: None)
    capsys.readouterr()
    new = [{"n": f"new{k}", "date": "2026-10-20", "who": f"New {k}", "body": f"A brand new review number {k} that says the robe is lovely and soft."} for k in range(13)]
    server.products["dude-robe"] = ("111", new + robe)
    server.asked.clear()
    assert reviews_cli.run_reviews(collect_args(), store, "2026-10-20T00:00:00Z", get=server.get, sleep=lambda s: None) == 0
    assert "up to date" in capsys.readouterr().out and server.asked == []                                                                              # 19 days: not due
    assert reviews_cli.run_reviews(collect_args(), store, "2026-11-05T00:00:00Z", get=server.get, sleep=lambda s: None) == 0
    out = capsys.readouterr().out
    widget = [u for u in server.asked if "reviews_for_widget" in u]
    assert len(widget) == 3 and "13 new" in out and R.load(store, "duderobe")["count"] == 38                                                           # page 1 and 2 hold new reviews, page 3 holds only known ones: caught up
    server.asked.clear()
    reviews_cli.run_reviews(collect_args(force=True), store, "2026-11-06T00:00:00Z", get=server.get, sleep=lambda s: None)
    assert len([u for u in server.asked if "reviews_for_widget" in u]) == 1 and "0 new" in capsys.readouterr().out                                    # forced: one page, nothing new


def test_a_run_that_reaches_its_time_limit_keeps_what_it_read_and_the_next_run_continues(tmp_path, capsys):
    products = {f"p{n}": (str(100 + n), make_reviews(f"p{n}", 12)) for n in range(4)}
    server = FakeJudgeMe(products)
    store = store_with_products(tmp_path, list(products))
    ticks = iter(range(0, 10 ** 6, 4000))                                                                                                              # the clock jumps 4000 s at each look
    reviews_cli.run_reviews(collect_args(time_budget=150), store, NOW, get=server.get, sleep=lambda s: None, clock=lambda: next(ticks))
    out = capsys.readouterr().out
    first = R.load(store, "duderobe")
    done = [h for h, e in first["collect"].items() if e["complete"]]
    assert 0 < len(done) < 4 and "stopped at the time limit (150 minutes)" in out and "the next run continues from there" in out and first["count"] == 12 * len(done)
    server.asked.clear()
    reviews_cli.run_reviews(collect_args(), store, NOW, get=server.get, sleep=lambda s: None)
    final = R.load(store, "duderobe")
    again = capsys.readouterr().out
    assert final["count"] == 48 and len(final["collect"]) == 4 and all(e["complete"] for e in final["collect"].values()) and f"read {4 - len(done)} of {4 - len(done)} product(s)" in again
    assert not any(f"/products/{h}" in u for h in done for u in server.asked)                                                                          # the products already read in full are not read again


def test_a_refusing_page_address_keeps_the_first_page_says_why_and_ends_the_run_after_three_refusals(tmp_path, capsys):
    products = {f"p{n}": (str(100 + n), make_reviews(f"p{n}", 12)) for n in range(5)}
    server = FakeJudgeMe(products)
    server.refuse = {"100", "101", "102", "103", "104"}
    store = store_with_products(tmp_path, list(products))
    reviews_cli.run_reviews(collect_args(), store, NOW, get=server.get, sleep=lambda s: None)
    out = capsys.readouterr().out
    data = R.load(store, "duderobe")
    assert "Judge.me's page address answered 403 on page 1" in out and "stopped: Judge.me's page address refused 3 products in a row" in out and "the first page of each product's reviews was still kept" in out
    assert data["count"] == 15 and not any(e["complete"] for e in data["collect"].values()) and len(data["collect"]) == 3                                  # 3 products x the 5 drawn into the page; none called complete, so the next run retries them


def test_a_product_page_with_no_widget_a_missing_page_and_the_page_limit_are_each_said(tmp_path, capsys, monkeypatch):
    server = FakeJudgeMe({"plain": ("1", [])})
    original = server.get
    server.get = lambda url: (200, "<html><body>no widget at all</body></html>") if url.endswith("/plain") else original(url)
    store = store_with_products(tmp_path, ["plain", "gone"])
    assert reviews_cli.run_reviews(collect_args(), store, NOW, get=server.get, sleep=lambda s: None) == 0
    out = capsys.readouterr().out
    assert "gone: the product page answered 404" in out and R.load(store, "duderobe")["collect"]["plain"]["note"].startswith("no Judge.me widget") and R.load(store, "duderobe")["collect"]["plain"]["complete"] is True
    monkeypatch.setattr(reviews_cli, "MAX_PAGES", 2)
    big = FakeJudgeMe({"big": ("9", make_reviews("big", 60))})
    store2 = store_with_products(tmp_path / "two", ["big"])
    reviews_cli.run_reviews(collect_args(), store2, NOW, get=big.get, sleep=lambda s: None)
    assert "big: stopped at the page limit (2 pages)" in capsys.readouterr().out and R.load(store2, "duderobe")["collect"]["big"]["complete"] is False


def test_the_page_parser_reads_the_json_answer_or_bare_html_and_the_shop_and_product_are_found_in_the_page():
    rows = make_reviews("x", 2)
    html = "".join(block(r) for r in rows)
    assert [r["reviewer"] for r in R.parse_page(json.dumps({"html": html}), "h", "u")] == ["x person 0", "x person 1"] and len(R.parse_page(html, "h", "u")) == 2
    assert R.parse_page("{not json", "h", "u") == [] and R.parse_page(json.dumps({"html": ""}), "h", "u") == [] and R.parse_page("", "h", "u") == []
    assert R.shop_info(PRODUCT_PAGE.format(pid="4455", reviews="")) == {"shop": "dude.myshopify.com", "product_id": "4455"} and R.shop_info("<html></html>") == {"shop": "", "product_id": ""}
    assert R.page_url("a.myshopify.com", "7", 3, 10) == "https://judge.me/reviews/reviews_for_widget?url=a.myshopify.com&shop_domain=a.myshopify.com&platform=shopify&page=3&per_page=10&product_id=7"


def test_a_first_reading_goes_on_past_reviews_it_already_holds_and_every_request_is_paced_and_products_not_yet_read_come_first(tmp_path, capsys):
    robe = make_reviews("robe", 25)
    server = FakeJudgeMe({"dude-robe": ("111", robe), "dude-wipes": ("222", make_reviews("wipes", 7))})
    store = store_with_products(tmp_path, ["dude-robe", "dude-wipes"])
    R.merge(store, "duderobe", [r for r in R.parse_page(json.dumps({"html": "".join(block(x) for x in robe[:10])}), "dude-robe")], "judge.me export", "2026-10-01T00:00:00Z")     # ten of the robe's reviews are already stored from another route
    slept = []
    reviews_cli.run_reviews(collect_args(pause=0.5), store, NOW, get=server.get, sleep=slept.append)
    capsys.readouterr()
    assert R.load(store, "duderobe")["count"] == 32 and R.load(store, "duderobe")["collect"]["dude-robe"]["pages"] == 3                                 # a first reading does not stop at reviews it already holds: it takes the whole history
    assert len(slept) == len(server.asked) and set(slept) == {0.5}                                                                                       # one pause after every single request
    server2 = FakeJudgeMe({"a-done": ("1", make_reviews("a", 3)), "b-new": ("2", make_reviews("b", 3))})
    store2 = store_with_products(tmp_path / "two", ["a-done", "b-new"])
    R.merge(store2, "duderobe", [], "x", "2026-09-01T00:00:00Z", progress={"a-done": {"complete": True, "pages": 1, "found": 3, "note": "", "read_at": "2026-09-01T00:00:00Z", "topup_at": "2026-09-01T00:00:00Z"}})
    reviews_cli.run_reviews(collect_args(), store2, NOW, get=server2.get, sleep=lambda s: None)
    capsys.readouterr()
    pages = [u.rsplit("/products/", 1)[1] for u in server2.asked if "/products/" in u]
    assert pages == ["b-new", "a-done"]                                                                                                                   # the product never read in full is read before the one only due for a top-up
