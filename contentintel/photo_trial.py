"""Photo trial: one photo per run, from Pollinations image generation (free,
no key, no signup), saved to the brand's own Drive folder and sheet.

Pollinations is the free image route that actually works: a plain GET to
image.pollinations.ai returns the image, no account, no key, no browser.
Probed live 2026-11-18 (HTTP 200, a real 512x512 JPEG in 7.7s, no auth).
This module makes one image per run — one prompt, one photo, hard cap — and
records a go/no-go verdict, the way the video trial does for one clip per
site. Output is a draft only — personal-use, publishing OFF.

What it never does: create an account, log in, spend money, generate more
than ONE photo per run, or touch the developer's files. It plugs into his
code the way an external connection does: it imports the tool's own Drive,
Sheets and brand code (contentintel/drive.py, contentintel/sheets.py,
contentintel/brands.py) and calls them — it never reimplements Drive or
Sheets handling, and it never hard-codes a folder or sheet id.

Run it: ``python -m contentintel.photo_trial --dry-run --brand duderobe``
first (plan only, no image is made, nothing is touched), then without
--dry-run for the real trial. One photo per run, hard cap, structural: the
run makes exactly one generation.

Where a made photo goes: ONLY to the brand the run is for. The photo is
saved to Shared Drive 'Idea Bank Automation' / Generated Content / Photo /
<Brand name> under the name
``YYYY-MM-DD_<brand>_pollinations_<prompt-slug>.jpg`` (or .png when the
returned image is a PNG), and one row is appended to that brand's own
Catalogue sheet, tab "Photos", with the Drive link. A photo for DudeRobe
never lands in Swoveralls' or Defi Snacks' folder or sheet. The save
reuses the tool's own Drive and Sheets code by import — it never copies
them and never hard-codes a folder or sheet id. A trial that makes no
photo records nothing in Drive or in the Sheet; its verdict is already in
photo_trial.json. A save that fails never loses the verdict: the trial
result stands, the exact error is printed and recorded, and the run still
exits 0.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import brands as brands_mod
from . import drive as drive_mod
from .store import Store

# One photo per run. Not a default to tune: the trial is one photo, and a
# second generation in the same run is the shape of hammering a free tier,
# not testing it. The cap is structural — run_trial makes exactly one call.
MAX_PHOTOS_PER_RUN = 1
SOURCE = "pollinations"
DEFAULT_MODEL = "flux"
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
# A generation takes seconds, not minutes, but a slow queue can stall.
FETCH_TIMEOUT_SECONDS = 120
DEFAULT_PROMPT = "A red bicycle leaning against a wooden fence in a sunny garden"


# ---------------------------------------------------------------------------
# The per-brand save: a made photo goes ONLY to the brand the run is for.
# ---------------------------------------------------------------------------

GENERATED_FOLDER = "Generated Content"
PHOTO_FOLDER = "Photo"
PHOTOS_TAB = "Photos"
PHOTOS_HEADERS = ["Date", "Brand", "Source", "Model", "Prompt", "Verdict",
                  "Photo", "Credits used", "Notes"]


class TrialError(Exception):
    """The run cannot proceed (bad brand, bad size, the generation failed,
    the save failed). Raised with a plain reason, never a traceback."""


def _slugify(text, limit=40):
    """A filename piece: lower case, runs of anything but letters and digits
    become one hyphen, no leading or trailing hyphen, never empty. A long
    piece is cut at a hyphen, never in the middle of a word."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    if len(slug) > limit:
        cut = slug[:limit]
        if "-" in cut:
            cut = cut[:cut.rfind("-")]
        slug = cut.rstrip("-")
    return slug or "photo"


def photo_filename(date_str, brand_slug, prompt, ext="jpg"):
    """YYYY-MM-DD_<brand>_pollinations_<prompt-slug>.<ext> — the brand is
    in the name, so a photo can never be mistaken for another brand's."""
    return (f"{date_str}_{brand_slug}_{_slugify(SOURCE, limit=30)}_"
            f"{_slugify(prompt)}.{ext}")


def resolve_brand(slug, brands_dir="brands"):
    """The brand profile for a slug, or a TrialError that says plainly what
    is wrong. The slug must be one of the tool's own brand keys."""
    try:
        return brands_mod.load_brand(slug, brands_dir)
    except (brands_mod.BrandError, OSError) as exc:
        known = brands_mod.list_brand_slugs(brands_dir)
        raise TrialError(f"unknown brand {slug!r}"
                         + (f" (known brands: {', '.join(known)})" if known else "")
                         + f": {exc}") from exc


def build_image_url(prompt, model=DEFAULT_MODEL, width=DEFAULT_WIDTH,
                    height=DEFAULT_HEIGHT, seed=None):
    """The Pollinations image URL for a prompt. The prompt is URL-encoded
    into the path; the model, size and nologo flag are query parameters.
    A seed is added only when one is given, so the same prompt without a
    seed varies, and with a seed repeats."""
    encoded = urllib.parse.quote(str(prompt), safe="")
    params = {"model": model, "width": str(width), "height": str(height),
              "nologo": "true"}
    if seed is not None:
        params["seed"] = str(seed)
    return f"{POLLINATIONS_BASE}/{encoded}?{urllib.parse.urlencode(params)}"


def detect_image(data):
    """("jpeg", "image/jpeg") or ("png", "image/png") from the first bytes,
    else None. Pollinations answers a JPEG for flux; a PNG is possible for
    other models, so the save names the file by what actually came back."""
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    return None


def _fetch_bytes(url, timeout=FETCH_TIMEOUT_SECONDS):
    """The raw bytes of one GET, with a plain User-Agent. Raises TrialError
    with the plain reason on any HTTP error, timeout or empty body."""
    request = urllib.request.Request(url, headers={"User-Agent": "content-strategy-tool-photo-trial/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise TrialError(f"Pollinations answered HTTP {response.status} for the image")
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise TrialError(f"Pollinations answered HTTP {exc.code} for the image"
                         + (f" ({exc.reason})" if exc.reason else "")) from exc
    except urllib.error.URLError as exc:
        raise TrialError(f"Pollinations could not be reached ({exc.reason})") from exc
    except TimeoutError as exc:
        raise TrialError(f"Pollinations did not answer within {timeout}s") from exc
    if not data:
        raise TrialError("Pollinations answered with an empty body")
    return data


def fetch_image(url, fetch=None, timeout=FETCH_TIMEOUT_SECONDS):
    """The image bytes for a URL. `fetch` is injected by tests; on a real
    run it is the plain GET above. Anything that is not a JPEG or a PNG is
    a TrialError, never a saved file — an error page must never land in a
    brand's Drive folder as a photo."""
    data = (fetch or _fetch_bytes)(url, timeout) if fetch is not None else _fetch_bytes(url, timeout)
    if detect_image(data) is None:
        raise TrialError("Pollinations answered, but not with an image "
                         f"({len(data)} bytes, not a JPEG or a PNG)")
    return data


def save_plan_lines(brand, prompt, run_date):
    """The dry-run plan for where the photo would go. No HTTP call is made
    and nothing is touched: the Drive path is folder names, and the sheet
    is named from the tool's own mapping."""
    filename = photo_filename(run_date, brand.slug, prompt)
    return [
        f"    save: Drive: Generated Content / Photo / {brand.name} / {filename} (under the IDEA_BANK_FOLDER_ID root)",
        f"    save: Sheet: the {brand.slug} Catalogue sheet (sheets.yaml), tab \"Photos\" — one row per saved photo",
    ]


class PhotoSaver:
    """Saves one trial photo to the brand's own Drive folder and records it
    in the brand's own sheet. The Drive folder and the sheet are found
    through the tool's own mapping (the brand's name for the folder,
    sheets.yaml for the sheet) — never a hard-coded id. `drive_client` /
    `sheets_client` / `store` are injected by tests; on a real run they are
    built from the run's secrets. save() raises TrialError with a plain
    reason when the Drive part fails; when only the Sheet row fails it
    returns the Drive link together with the reason, so the photo is never
    lost silently."""

    def __init__(self, brand, env, data_dir="data", sheets_config="sheets.yaml",
                 brands_dir="brands", drive_client=None, sheets_client=None, store=None):
        self.brand = brand
        self.env = env
        self.data_dir = data_dir
        self.sheets_config = sheets_config
        self.brands_dir = brands_dir
        self._drive = drive_client
        self._sheets = sheets_client
        self._sheets_email = "test@example.invalid"
        self._store = store

    def save(self, model, prompt, photo_rel_path, out_dir, run_date):
        """(drive_link, sheet_error): the Drive link when the photo reached
        the brand's folder (None when it did not), and the plain reason the
        Sheet row was not written (None when it was)."""
        photo_path = Path(out_dir) / photo_rel_path
        try:
            data = photo_path.read_bytes()
        except OSError as exc:
            raise TrialError(f"the made photo {photo_rel_path} could not be read ({exc})") from exc
        found = detect_image(data)
        if found is None:
            raise TrialError(f"the made photo {photo_rel_path} is not a JPEG or a PNG")
        ext, mime = found
        filename = photo_filename(run_date, self.brand.slug, prompt, ext)
        link = self._save_to_drive(filename, data, mime)
        try:
            self._record_in_sheet(model, prompt, run_date, link)
        except TrialError as exc:
            return link, str(exc)
        return link, None

    # ---- Drive -----------------------------------------------------------------

    def _drive_client(self):
        if self._drive is not None:
            return self._drive
        key = (self.env.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
        if not key:
            raise TrialError("GOOGLE_SERVICE_ACCOUNT_JSON is not set, so the photo cannot be saved to Drive; "
                             "add the service-account key as a secret")
        try:
            root = drive_mod.parse_folder_id(self.env.get("IDEA_BANK_FOLDER_ID", ""))
        except drive_mod.DriveError as exc:
            raise TrialError(str(exc)) from exc
        if not root:
            raise TrialError("IDEA_BANK_FOLDER_ID is not set, so the brand's Drive folder cannot be found; "
                             "add the Idea Bank folder id as a secret")
        try:
            session, email = drive_mod.connect(key)
        except drive_mod.DriveError as exc:
            raise TrialError(str(exc)) from exc
        self._drive = drive_mod.DriveClient(session, root, email)
        return self._drive

    def _save_to_drive(self, filename, data, mime):
        """The photo in Generated Content / Photo / <Brand name>. A file with
        the same name (the same trial run twice) is trashed first, so one name
        never holds two photos; the trash is best-effort. The bytes are
        verified after the upload, the way ideabank._upload_verified does it."""
        try:
            client = self._drive_client()
            generated_id = client.ensure_folder(client.root_id, GENERATED_FOLDER)
            media_id = client.ensure_folder(generated_id, PHOTO_FOLDER)
            brand_id = client.ensure_folder(media_id, self.brand.name)
            existing = client.list_files(brand_id)
            if filename in existing:
                try:
                    client.trash(existing[filename])
                except drive_mod.DriveError:
                    pass
            file_id = client.upload(brand_id, filename, data, mime)
            try:
                client.verify(file_id, data)
            except drive_mod.DriveMismatch as exc:
                raise TrialError(f"Drive holds a different file from the one sent ({exc})") from exc
            return drive_mod.file_url(file_id)
        except drive_mod.DriveError as exc:
            raise TrialError(str(exc)) from exc

    # ---- Sheet -----------------------------------------------------------------

    def _sheets_client(self):
        if self._sheets is not None:
            return self._sheets, self._sheets_email
        from . import sheets as sheets_mod
        key = (self.env.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
        if not key:
            raise TrialError("GOOGLE_SERVICE_ACCOUNT_JSON is not set, so the Sheet row cannot be written; "
                             "add the service-account key as a secret")
        try:
            client, email = sheets_mod.connect(key)
        except sheets_mod.SheetsError as exc:
            raise TrialError(str(exc)) from exc
        return client, email

    def _record_in_sheet(self, model, prompt, run_date, link):
        """One row in the brand's own sheet, tab "Photos". The tab is
        created with its header when it is missing; a tab whose header was
        changed by hand is left alone and the save fails plainly, so a row
        is never written under the wrong columns."""
        from . import sheets as sheets_mod
        from gspread.exceptions import GSpreadException
        from gspread.utils import ValueInputOption
        try:
            client, email = self._sheets_client()
            known = set(brands_mod.list_brand_slugs(self.brands_dir)) or {self.brand.slug}
            config = sheets_mod.load_sheet_config(self.sheets_config, known)
            store = self._store or Store(self.data_dir)
            report = sheets_mod.SheetReport(brand=self.brand.slug)
            folder_id = sheets_mod.parse_folder_id(self.env.get("SHEET_FOLDER_ID", ""))
            sp = sheets_mod.resolve_spreadsheet(client, self.brand, store, config, [],
                                               report, False, email, folder_id=folder_id)
            ws = None
            for candidate in sp.worksheets():
                if candidate.title == PHOTOS_TAB:
                    ws = candidate
                    break
            if ws is None:
                ws = sp.add_worksheet(title=PHOTOS_TAB, rows=1000, cols=30)
                self._write_header(ws)
            else:
                values = ws.get_all_values()
                if not values:
                    self._write_header(ws)
                else:
                    header = [str(h) for h in values[0]]
                    while header and header[-1] == "":
                        header.pop()
                    if header != PHOTOS_HEADERS:
                        raise TrialError(
                            f"the {PHOTOS_TAB!r} tab in the {self.brand.slug} sheet has a different header "
                            f"({', '.join(header) or 'blank'}); fix the header by hand or delete the tab and run again")
            row = [run_date, self.brand.slug, SOURCE, model, prompt, "go", link, "", ""]
            ws.append_rows([row], value_input_option=ValueInputOption.raw, table_range="A1")
        except (sheets_mod.SheetsError, GSpreadException) as exc:
            raise TrialError(str(exc)) from exc

    @staticmethod
    def _write_header(ws):
        ws.update(values=[list(PHOTOS_HEADERS)], range_name="A1")
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})


# ---------------------------------------------------------------------------
# The run: plan (dry run) or trial
# ---------------------------------------------------------------------------

VERDICTS = ("go", "no-go", "skipped")


def verdict(source, state, reason, photo=None, seconds=0.0):
    """One trial's verdict. "go" means one photo was made and downloaded;
    "no-go" means the trial ran and the generation failed it; "skipped"
    means it was not attempted (dry run). A no-go is a successful trial
    finding, not a run failure. The key is "verdict", the way the video
    trial's verdicts are written, so both trial JSONs read the same."""
    if state not in VERDICTS:
        raise TrialError(f"unknown verdict {state!r}")
    entry = {"source": source, "model": DEFAULT_MODEL, "verdict": state,
             "reason": reason, "seconds": round(seconds, 1)}
    if photo is not None:
        entry["photo"] = photo
    return entry


def verdict_line(entry):
    """One readable line for a verdict, with the Drive link when the photo
    was saved."""
    line = f"photo-trial: {entry['source']}: {entry['verdict'].upper()} — {entry['reason']}"
    if entry.get("photo"):
        line += f" (photo: {entry['photo']})"
    if entry.get("drive_link"):
        line += f" — saved to the brand's Drive: {entry['drive_link']}"
    if entry.get("save_error"):
        line += f" (Save failed: {entry['save_error']})"
    return line


def run_trial(prompt, brand, env, out_dir, verdicts_path=None,
              dry_run=False, fetcher=None, saver=None,
              now=None, data_dir="data", sheets_config="sheets.yaml",
              model=DEFAULT_MODEL, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
              seed=None):
    """Run the trial: one photo, one verdict. Returns (verdicts, exit_code).
    Exit 0 when the trial got a verdict — a no-go is a successful trial
    finding, not a run failure. Exit 1 only when the run itself broke (the
    verdicts could not be written). `brand` is the resolved brand the run
    is for; without it the run refuses to start, so a photo can never be
    made for no brand at all."""
    if brand is None:
        raise TrialError("no brand was given; the trial must know which brand it is for "
                         "before it generates anything")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_date = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y-%m-%d")
    verdicts = []
    if dry_run:
        print("photo-trial: dry run — the plan only, no image is made")
        print(f"  brand: {brand.slug} ({brand.name}) — photos go only to this brand's Drive folder and sheet")
        print(f"  {SOURCE} ({model}, {width}x{height}):")
        print("    free: no key, no signup; no watermark when nologo is set; non-commercial")
        print("    - one GET to image.pollinations.ai, the image bytes back")
        for line in save_plan_lines(brand, prompt, run_date):
            print(line)
        verdicts.append(verdict(SOURCE, "skipped", "dry run — no image made"))
    else:
        started = time.time()
        url = build_image_url(prompt, model, width, height, seed)
        try:
            data = fetch_image(url, fetch=fetcher)
        except TrialError as exc:
            verdicts.append(verdict(SOURCE, "no-go", str(exc),
                                    seconds=time.time() - started))
            print(verdict_line(verdicts[-1]))
        else:
            found = detect_image(data)
            ext = found[0]
            rel_path = photo_filename(run_date, brand.slug, prompt, ext)
            path = out_dir / rel_path
            path.write_bytes(data)
            entry = verdict(SOURCE, "go", f"one {model} photo made and downloaded",
                            photo=rel_path, seconds=time.time() - started)
            if saver is None:
                saver = PhotoSaver(brand, env, data_dir=data_dir,
                                   sheets_config=sheets_config)
            try:
                link, sheet_error = saver.save(model, prompt, rel_path, out_dir, run_date)
            except TrialError as exc:
                print(f"photo-trial: {SOURCE}: Drive save failed — {exc} (the photo stays in the run's files)")
                entry["save_error"] = str(exc)
            else:
                if sheet_error:
                    print(f"photo-trial: {SOURCE}: photo saved to Drive ({link}) but the Sheet row was not written — {sheet_error}")
                    entry["save_error"] = sheet_error
                else:
                    print(f"photo-trial: {SOURCE}: saved to the brand's Drive — {link}")
                entry["drive_link"] = link
            verdicts.append(entry)
            print(verdict_line(entry))
    result = {"run_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "prompt": prompt, "brand": brand.slug, "source": SOURCE,
              "model": model, "dry_run": dry_run, "verdicts": verdicts}
    target = Path(verdicts_path) if verdicts_path else out_dir / "photo_trial.json"
    try:
        if target.parent != Path("."):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"photo-trial: could not write the verdicts to {target} ({exc})")
        return verdicts, 1
    print(f"photo-trial: verdicts written to {target}")
    return verdicts, 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m contentintel.photo_trial",
        description="One photo per run, from Pollinations image generation "
                    "(free, no key, no signup), saved to the brand's own "
                    "Drive folder and sheet. Draft only, publishing OFF.")
    parser.add_argument("--brand", required=True,
                        help="the brand this trial is for (swoveralls, duderobe or defi_snacks); "
                             "the photo goes ONLY to that brand's Drive folder and sheet")
    parser.add_argument("--brands-dir", default="brands",
                        help="the folder holding the brand profiles (default: brands)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help="the prompt for the one photo (default: a brand-neutral scene)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="the Pollinations image model (default: flux)")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help="the photo width in pixels (256–2048, default: 1024)")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                        help="the photo height in pixels (256–2048, default: 1024)")
    parser.add_argument("--seed", type=int, default=None,
                        help="repeat the same photo for the same prompt (default: a new photo each run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan only; never make an image")
    parser.add_argument("--out", default="debug/photo-trial",
                        help="where the photo and the verdicts go")
    parser.add_argument("--verdicts", default=None,
                        help="where to write photo_trial.json (default: <out>/photo_trial.json)")
    args = parser.parse_args(argv)
    if not args.prompt.strip():
        print("photo-trial: the prompt is empty; give one with --prompt")
        return 1
    if not 256 <= args.width <= 2048 or not 256 <= args.height <= 2048:
        print("photo-trial: --width and --height must each be 256–2048")
        return 1
    try:
        brand = resolve_brand(args.brand, args.brands_dir)
    except TrialError as exc:
        print(f"photo-trial: {exc}")
        return 1
    _, code = run_trial(args.prompt, brand, os.environ, args.out,
                        verdicts_path=args.verdicts, dry_run=args.dry_run,
                        model=args.model, width=args.width, height=args.height,
                        seed=args.seed)
    return code


if __name__ == "__main__":
    sys.exit(main())
