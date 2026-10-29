"""Careful mode for Instagram is OFF in the shipped browser.yaml (it limits what a run can collect), and a person can switch it on by removing
the # from four lines. The careful-mode tests exercise exactly that: they run against a copy of the shipped file with those four lines switched on."""
import re
from pathlib import Path

CAREFUL_KEYS = ("pacing", "max_scrolls_per_page", "max_pages_per_run", "max_pages_per_day")


def careful_text(text):
    """The shipped browser.yaml with Instagram's careful-mode lines switched on."""
    for key in CAREFUL_KEYS:
        text, n = re.subn(rf"^(\s*)# ({key}:)", r"\1\2", text, flags=re.M)
        assert n == 1, key
    return text


def careful_yaml(directory):
    path = Path(directory) / "browser_careful.yaml"
    path.write_text(careful_text(Path("browser.yaml").read_text(encoding="utf-8")), encoding="utf-8")
    return str(path)
