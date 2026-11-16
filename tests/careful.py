"""Careful mode for Instagram is ON in the shipped browser.yaml since Update 68 (it was off before; the owner chose slower, smaller, spread-out reads after an automation warning). These helpers still switch the four lines on if a copy has them commented. The careful-mode tests exercise exactly that: they run against a copy of the shipped file with those four lines switched on."""
import re
from pathlib import Path

CAREFUL_KEYS = ("pacing", "max_scrolls_per_page", "max_pages_per_run", "max_pages_per_day")


def careful_text(text, gap=False):
    """The shipped browser.yaml with Instagram's careful-mode lines switched on. The 12-hour gap between runs (min_hours_between_runs) is taken out unless gap=True: the caps tests run several runs on one simulated day."""
    if not gap:
        text = re.sub(r"^\s*min_hours_between_runs:.*\n", "", text, flags=re.M)
    for key in CAREFUL_KEYS:
        text = re.sub(rf"^(\s*)# ({key}:)", r"\1\2", text, flags=re.M)             # since Update 68 the shipped file already has them on: switching on is then a no-op
        assert len(re.findall(rf"^\s*{key}:", text, flags=re.M)) >= 1, key
    return text


def careful_yaml(directory):
    path = Path(directory) / "browser_careful.yaml"
    path.write_text(careful_text(Path("browser.yaml").read_text(encoding="utf-8")), encoding="utf-8")
    return str(path)
