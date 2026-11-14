"""Human-like browsing for the sessions that are logged in with an account (Instagram, TikTok, and any platform connected the same way).

A person does not open a page after a fixed pause and jump two thousand pixels down it. This module gives the browser the habits of one: pauses that vary the way reading time varies (log-normal, so most are short and a few are long),
an occasional longer pause as if the person looked away, a moment to settle on a page after it opens, a mouse that moves, a scroll made of several small wheel turns that each take a moment (and now and then a little scroll back up),
and a longer break after every few pages. It only ever looks: it moves the mouse, turns the wheel and waits. It never clicks, likes, follows, posts or messages, so nothing it does changes the account.

`enabled: false` under `human:` in browser.yaml brings back the plain pauses and jumps exactly as they were. Nothing here reads a clock or a random source directly: the random generator and the sleep are passed in, so the behaviour is tested without waiting."""
import math
import random
import time

DEFAULTS = {
    "enabled": True,
    "settle_seconds": [1.5, 4.0],           # looking at the page after it has opened
    "wheel_steps": [5, 9],                  # one scroll is this many small wheel turns
    "wheel_step_pixels": [120, 420],
    "wheel_step_ms": [40, 160],             # the moment each turn takes
    "scroll_back_chance": 0.12,             # now and then a little scroll back up
    "scroll_back_pixels": [80, 300],
    "reading_variation": 0.35,              # how much reading time varies (spread of the log-normal)
    "distraction_chance": 0.08,             # now and then a longer pause, as when someone looks away
    "distraction_factor": [2.0, 4.0],
    "mouse_moves": True,
    "break_every_pages": [6, 10],           # a longer break after this many pages
    "break_seconds": [20, 60],
}
_RANGES = ("settle_seconds", "wheel_steps", "wheel_step_pixels", "wheel_step_ms", "scroll_back_pixels", "distraction_factor", "break_every_pages", "break_seconds")
_CHANCES = ("scroll_back_chance", "distraction_chance")
VIEWPORT = (1366, 900)
LONGEST_PAUSE = 180.0                       # no single pause is longer than this many seconds


class HumanConfigError(ValueError):
    pass


def resolve(block):
    """The `human:` block of browser.yaml merged over the defaults and checked. None or {} means the defaults (on)."""
    block = {} if block is None else block
    if not isinstance(block, dict):
        raise HumanConfigError("browser.yaml human: must be a block of settings")
    unknown = sorted(set(block) - set(DEFAULTS))
    if unknown:
        raise HumanConfigError(f"browser.yaml human: unknown {unknown}")
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    cfg.update(block)
    for key in ("enabled", "mouse_moves"):
        if not isinstance(cfg[key], bool):
            raise HumanConfigError(f"human.{key} must be true or false")
    for key in _RANGES:
        pair = cfg[key]
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in pair) and pair[0] <= pair[1]):
            raise HumanConfigError(f"human.{key} must be two numbers, the smaller first, for example {DEFAULTS[key]}")
    for key in _CHANCES:
        if not (isinstance(cfg[key], (int, float)) and not isinstance(cfg[key], bool) and 0 <= cfg[key] <= 1):
            raise HumanConfigError(f"human.{key} must be a number from 0 to 1")
    if not (isinstance(cfg["reading_variation"], (int, float)) and not isinstance(cfg["reading_variation"], bool) and 0 <= cfg["reading_variation"] <= 2):
        raise HumanConfigError("human.reading_variation must be a number from 0 to 2")
    if cfg["wheel_steps"][0] < 1 or cfg["break_every_pages"][0] < 1:
        raise HumanConfigError("human.wheel_steps and human.break_every_pages must start at 1 or more")
    return cfg


class Human:
    def __init__(self, block=None, rng=None, sleep=time.sleep):
        self.cfg = resolve(block)
        self.rng = rng if rng is not None else random.Random()
        self.sleep = sleep
        self.pages = 0
        self._next_break = self._pick_break()

    @property
    def enabled(self):
        return self.cfg["enabled"]

    def _between(self, key):
        low, high = self.cfg[key]
        return self.rng.uniform(low, high)

    def _int_between(self, key):
        low, high = self.cfg[key]
        return self.rng.randint(int(low), int(high))

    def _pick_break(self):
        return self._int_between("break_every_pages")

    def reading_time(self, median, floor, ceiling):
        """A pause around `median` seconds that varies like reading time: log-normal, kept between floor and ceiling, with now and then a longer one (the person looked away)."""
        seconds = self.rng.lognormvariate(math.log(max(median, 0.05)), self.cfg["reading_variation"])
        seconds = min(max(seconds, floor), ceiling)
        if self.rng.random() < self.cfg["distraction_chance"]:
            seconds *= self._between("distraction_factor")
        return min(seconds, LONGEST_PAUSE)

    def before_page(self, pacing):
        """The wait before a page is opened (and, after every few pages, a longer break). Returns the seconds waited in all."""
        low, high = pacing["min_delay_seconds"], pacing["max_delay_seconds"]
        waited = self.reading_time((low + high) / 2, low, high * 2)
        self.sleep(waited)
        return waited + self.count_page()

    def count_page(self):
        """Count one more page (or run) and, after every few, take the longer break. Returns the seconds slept in the break (0 when none was due)."""
        self.pages += 1
        if self.pages < self._next_break:
            return 0.0
        pause = self._between("break_seconds")
        self.sleep(pause)
        self.pages, self._next_break = 0, self._pick_break()
        return pause

    def _move_mouse(self, page, near=None):
        width, height = VIEWPORT
        x, y = (self.rng.randint(120, width - 120), self.rng.randint(140, height - 140)) if near is None else (min(max(near[0] + self.rng.randint(-60, 60), 40), width - 40), min(max(near[1] + self.rng.randint(-40, 40), 40), height - 40))
        page.mouse.move(x, y, steps=self.rng.randint(8, 22))
        return x, y

    def after_load(self, page):
        """A moment to settle on the page, and a mouse that is not standing still."""
        page.wait_for_timeout(int(self._between("settle_seconds") * 1000))
        if self.cfg["mouse_moves"]:
            self._move_mouse(page)

    def scroll(self, page, times, pause):
        """`times` scrolls. Each is several small wheel turns, now and then a little back up, then a reading pause around `pause` seconds. Returns the pixels turned down in all."""
        total, spot = 0, None
        for _ in range(times):
            for _ in range(self._int_between("wheel_steps")):
                pixels = self._int_between("wheel_step_pixels")
                page.mouse.wheel(0, pixels)
                total += pixels
                page.wait_for_timeout(self._int_between("wheel_step_ms"))
            if self.rng.random() < self.cfg["scroll_back_chance"]:
                back = self._int_between("scroll_back_pixels")
                page.mouse.wheel(0, -back)
                total -= back
                page.wait_for_timeout(int(self.rng.uniform(300, 700)))
            if self.cfg["mouse_moves"]:
                spot = self._move_mouse(page, spot)
            page.wait_for_timeout(int(self.reading_time(pause, pause * 0.5, pause * 3) * 1000))
        return total
