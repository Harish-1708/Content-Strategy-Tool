"""A stand-in for a real browser, and sample data shaped like what the sites send to their own pages."""
from contextlib import contextmanager

from contentintel.browser import BrowserError

META_JSON = {"data": {"ad_library_main": {"search_results_connection": {"edges": [{"node": {"collated_results": [
    {"ad_archive_id": "111", "page_name": "Acme", "start_date": 1760000000,
     "snapshot": {"body": {"text": "Soft  overalls\n for everyone"}, "title": "Shop now",
                  "images": [{"original_image_url": "https://img.example/1.jpg"}], "videos": []}},
    {"ad_archive_id": "112", "page_name": "Acme", "start_date": 1760086400,
     "snapshot": {"body": {"text": "Second ad"}, "cards": [{"body": "Card text", "title": "T"}]}}]}}]}}}}
IG_JSON = {"media": [
    {"pk": "9001", "code": "abc", "taken_at": 1760000000, "caption": {"text": "Weekend overalls"}, "like_count": 50,
     "comment_count": 3, "user": {"username": "acme"}, "media_type": 1,
     "image_versions2": {"candidates": [{"url": "https://img.example/i.jpg"}]}},
    {"pk": "9002", "code": "def", "taken_at": 1760001000, "caption": None, "like_count": 7, "comment_count": 0,
     "user": {"username": "bee"}, "media_type": 2, "product_type": "clips", "play_count": 1234}]}
TT_JSON = {"itemList": [
    {"id": "7001", "desc": "my overalls haul", "createTime": 1760000000, "author": {"uniqueId": "acme", "nickname": "Acme"},
     "stats": {"diggCount": 100, "shareCount": 5, "commentCount": 10, "playCount": 1000},
     "video": {"cover": "https://img.example/c.jpg"}},
    {"id": "7002", "desc": "second video", "createTime": 1760001000, "author": {"uniqueId": "bee"},
     "stats": {"diggCount": 9, "shareCount": 0, "commentCount": 1, "playCount": 90}}]}
REPORT_TEXT = ("Competitor analysis. " * 120) + "Sources: https://a.example.com/x and https://b.example.com/y."


class FakeBrowser:
    """`pages` maps an address prefix to what that page shows and sends."""

    def __init__(self, pages=None, *, clickable=(), typeable=(), present_always=()):
        self.pages = pages or {}
        self.spec, self._json, self._batches = {}, [], []
        self.opened, self.waits, self.clicks, self.typed, self.shots = [], [], [], [], []
        self.scrolls, self.clock, self.url = 0, 0, ""
        self.clickable, self.typeable, self.present_always = set(clickable), set(typeable), set(present_always)

    def open(self, url):
        self.opened.append(url)
        self.url = url
        match = max((k for k in self.pages if url.startswith(k)), key=len, default=None)
        self.spec = self.pages[match] if match else {}
        self._json = list(self.spec.get("json", []))
        self._batches = list(self.spec.get("scroll_json", []))

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock += seconds

    def scroll(self, times=1, pause=3):
        for _ in range(times):
            self.scrolls += 1
            if self._batches:
                self._json.extend(self._batches.pop(0))

    def json_responses(self):
        return list(self._json)

    def embedded_json(self):
        return list(self.spec.get("embedded", []))

    def links(self, contains):
        return [l for l in self.spec.get("links", []) if any(c in l["href"] for c in contains)]

    def dynamic_present(self):
        return set()

    def exists(self, selectors):
        here = self.present_always | set(self.spec.get("present", ())) | self.dynamic_present()
        return any(s in here for s in selectors)

    def on_click(self, selector):
        pass

    def click(self, selectors):
        for s in selectors:
            if s in self.clickable:
                self.clicks.append(s)
                self.on_click(s)
                return True
        return False

    def on_type(self, text):
        pass

    def type_text(self, selectors, text, submit=False):
        for s in selectors:
            if s in self.typeable:
                self.typed.append(text)
                self.on_type(text)
                return True
        return False

    def dynamic_text(self):
        return self.spec.get("text", "")

    def text(self, selectors):
        return self.dynamic_text()

    def response_links(self, selectors):
        return list(self.spec.get("response_links", []))

    def html(self):
        return "<html></html>"

    def screenshot(self, name):
        self.shots.append(name)
        return name


class GeminiFake(FakeBrowser):
    """Gemini's web app: a plan appears, 'Start research' is pressed, a report grows, then finishes."""

    def __init__(self, cfg, *, start_after=30, done_after=700, report=REPORT_TEXT, done_button=True,
                 logged_in=True, deep_button=True, stay_busy=False):
        flat = [s for group in cfg["deep_research_clicks"] for s in group] if deep_button else []
        super().__init__(clickable=set(flat) | set(cfg["send"]) | set(cfg["start_research"]),
                         typeable=set(cfg["prompt_box"]),
                         present_always={cfg["logged_in_any"][0], cfg["prompt_box"][0]} if logged_in else set(cfg["login_wall_any"][:1]))
        self.cfg, self.start_after, self.done_after = cfg, start_after, done_after
        self.report, self.done_button, self.stay_busy = report, done_button, stay_busy
        self.sent_at = self.started_at = None

    def click(self, selectors):
        if all(s in self.cfg["start_research"] for s in selectors) and not self.exists(selectors):
            return False                                  # a button that is not on the page cannot be pressed
        return super().click(selectors)

    def on_click(self, selector):
        if selector in self.cfg["send"]:
            self.sent_at = self.clock
        if selector in self.cfg["start_research"]:
            self.started_at = self.clock

    def dynamic_present(self):
        out = set()
        if self.sent_at is not None and self.started_at is None and self.clock - self.sent_at >= self.start_after:
            out.add(self.cfg["start_research"][0])
        if self.started_at is not None:
            if self.stay_busy or self.clock - self.started_at < self.done_after:
                out.add(self.cfg["still_working_any"][0])
            elif self.done_button:
                out.add(self.cfg["done_any"][0])
        return out

    def dynamic_text(self):
        if self.started_at is None:
            return ""
        share = min(1.0, (self.clock - self.started_at) / self.done_after) if self.done_after else 1.0
        return self.report[:int(len(self.report) * share)]

    def response_links(self, selectors):
        return ["https://c.example.com/z", "https://a.example.com/x"]


class Factory:
    """What the collectors call to get a browser for a site."""

    def __init__(self, browsers, fail=None):
        self.browsers, self.fail, self.opened = browsers, dict(fail or {}), []

    def __call__(self, site):
        return self._open(site)

    @contextmanager
    def _open(self, site):
        if site in self.fail:
            raise BrowserError(self.fail[site])
        self.opened.append(site)
        yield self.browsers[site]


class AskingGemini(GeminiFake):
    """A Gemini that asks clarifying questions before it offers a plan (`linger`: how long the old
    question stays on screen after a reply, as a slow Gemini would)."""

    def __init__(self, cfg, questions, linger=0, **kw):
        super().__init__(cfg, **kw)
        self.questions, self.linger, self.sends = list(questions), linger, 0

    def on_click(self, selector):
        if selector in self.cfg["send"]:
            self.sends += 1
            self.sent_at = self.clock
        if selector in self.cfg["start_research"]:
            self.started_at = self.clock

    def _asking(self):
        return 1 <= self.sends <= len(self.questions)

    def dynamic_present(self):
        out = set()
        if self.started_at is None:
            if self.sends > len(self.questions) and self.sent_at is not None and self.clock - self.sent_at >= self.start_after:
                out.add(self.cfg["start_research"][0])
            return out
        if self.clock - self.started_at < self.done_after:
            out.add(self.cfg["still_working_any"][0])
        elif self.done_button:
            out.add(self.cfg["done_any"][0])
        return out

    def dynamic_text(self):
        if self.started_at is None:
            if self._asking():
                return self.questions[self.sends - 1]
            if self.sends > len(self.questions):
                if self.questions and self.clock - self.sent_at < self.linger:
                    return self.questions[-1]
                return "Here is my research plan: step one, step two."
            return ""
        return super().dynamic_text()


class FakeClaude:
    """Claude, scripted: what it prepares, and what it answers to each of Gemini's messages."""
    model = "claude-sonnet-5-5"

    def __init__(self, prepared="PREPARED PROMPT", answers=None, fail_prepare=None, fail_answer=None):
        self.prepared, self.answers = prepared, list(answers or [])
        self.fail_prepare, self.fail_answer = fail_prepare, fail_answer
        self.prepare_calls, self.answer_calls = [], []
        self.completions, self.complete_calls, self.fail_complete = [], [], None

    def complete(self, system, user, max_tokens=None):
        self.complete_calls.append((system, user, max_tokens))
        if self.fail_complete:
            raise self.fail_complete
        return self.completions.pop(0) if self.completions else "{}"

    def prepare_prompt(self, question, context):
        self.prepare_calls.append((question, context))
        if self.fail_prepare:
            raise self.fail_prepare
        return self.prepared

    def answer(self, original_prompt, context, gemini_message):
        self.answer_calls.append((original_prompt, context, gemini_message))
        if self.fail_answer:
            raise self.fail_answer
        return self.answers.pop(0) if self.answers else ""

    def check(self):
        return True
