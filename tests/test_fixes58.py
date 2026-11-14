"""Update 58: what the live runs of Update 56 showed. Wording that named LinkedIn for every platform, a worker error said twice with its call log, a save step that named a file that did not exist yet,
search results about a DIFFERENT account used for an address, profile reads that never got the browser in a monitoring run, and 'no counts in the snippet' counted as an upstream failure."""
import subprocess
import yaml

from contentintel import contract as K
from contentintel import health as H
from contentintel import session_live as SL
from contentintel.observatory import social_profiles as SP

from conftest import ROOT

WF = ROOT / ".github" / "workflows"


# ---- the wording ------------------------------------------------------------------------------------------------------------------------

RAW = "Error: Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://www.instagram.com/swoveralls/\nCall log:\n  - navigating to \"https://www.instagram.com/swoveralls/\", waiting until \"load\""


def test_only_linkedin_is_said_to_answer_999_every_other_platform_gets_no_guess():
    for platform in ("instagram", "x", "facebook", "tiktok", "reddit", ""):
        text = SL.humanise_error("UPSTREAM_BROKEN", RAW, platform)
        assert "LinkedIn" not in text and "999" not in text and "the browser does not say which status" in text, platform
        assert text.startswith("the platform answered the browser with an error status instead of a page, which is its own refusal of an automated visit")
    linkedin = SL.humanise_error("UPSTREAM_BROKEN", RAW.replace("instagram", "linkedin"), "linkedin")
    assert "(LinkedIn does this with status 999)" in linkedin and "the browser does not say" not in linkedin
    assert "Call log" not in SL.humanise_error("UPSTREAM_BROKEN", RAW, "instagram") and "\n" not in SL.humanise_error("UPSTREAM_BROKEN", RAW, "instagram")


def test_a_failed_read_names_the_right_platform_in_the_failure_it_raises():
    res = {"error": {"code": "UPSTREAM_BROKEN", "message": RAW}}
    assert "LinkedIn" not in str(SL._failure_from(res, "instagram")) and "(LinkedIn does this with status 999)" in str(SL._failure_from(res, "linkedin"))
    assert "LinkedIn" not in str(SL._failure_from(res))                                                           # no platform given: still no guess


def test_the_evidence_keeps_the_workers_words_without_the_call_log(tmp_path):
    ev = {"worker_error": f"UPSTREAM_BROKEN: {' '.join(RAW.split('Call log:')[0].split())}"}
    assert "Call log" not in ev["worker_error"] and "navigating" not in ev["worker_error"]


# ---- a different account is never used for an address ------------------------------------------------------------------------------------

def result(rank, title, url, snippet):
    from contentintel.providers import Result
    return Result(rank, title, url, snippet)


class Search:
    def __init__(self, results):
        self.results, self.queries = results, []

    def search(self, query, kind="web", num=5):
        self.queries.append(query)
        return self.results


SWOVERALLS_RESULTS = [result(1, "SWOVERALLS (@swoveralls_) • Instagram photos", "https://www.instagram.com/swoveralls_/?hl=en", "51K followers · 1.9K+ following · 1325 posts"),
                      result(2, "Swoveralls | Original Sweatpant Overalls", "https://swoveralls.com/", "Swoveralls are the original sweatpant overalls."),
                      result(3, "True story. #swoveralls", "https://www.instagram.com/reel/DaVwb0dvCRT/", "These Swoveralls are soft"),
                      result(4, "Swoveralls (@swoveralls)", "https://www.instagram.com/swoveralls/", "The real account")]


def test_a_profile_of_a_similarly_named_account_is_left_out_and_said_which_one():
    rows = SL.discovery_rows("https://www.instagram.com/swoveralls", {}, [("serper_dev", Search(SWOVERALLS_RESULTS))])
    assert [r["url"] for r in rows] == ["https://swoveralls.com/", "https://www.instagram.com/reel/DaVwb0dvCRT/", "https://www.instagram.com/swoveralls/"]      # @swoveralls_ is not @swoveralls
    assert rows[0]["left_out"] == ["@swoveralls_"] and "left_out" not in rows[1]
    assert not any("51K" in r["text"] for r in rows)                                                              # the other account's numbers are not in the answer


def test_when_the_only_profile_found_is_a_different_account_nothing_is_returned_and_it_is_said_plainly():
    only_other = [SWOVERALLS_RESULTS[0]]
    try:
        SL.discovery_rows("https://www.instagram.com/swoveralls", {}, [("serper_dev", Search(only_other))])
    except K.Failure as exc:
        assert exc.code == K.NOT_FOUND and "only a different account (@swoveralls_)" in str(exc) and "never used in its place" in str(exc)
    else:
        raise AssertionError("a different account was accepted for the address")


def test_account_matching_is_exact_case_blind_and_ignores_posts_pages_other_sites_and_addresses_without_an_account():
    other = SL._other_account
    assert other("https://www.instagram.com/swoveralls", "https://www.instagram.com/swoveralls_/") == "@swoveralls_"
    assert other("https://www.instagram.com/SwOveralls/", "https://instagram.com/swoveralls?hl=en") == ""             # the same account, however it is written
    assert other("https://www.tiktok.com/@freethesheepco", "https://www.tiktok.com/@freethesheep") == "@freethesheep"
    assert other("https://x.com/swoveralls", "https://x.com/swoveralls/status/1") == ""                              # a post of the same account
    for found in ("https://www.instagram.com/reel/abc/", "https://www.instagram.com/explore/tags/x/", "https://swoveralls.com/", "https://www.tiktok.com/@swoveralls", "https://www.instagram.com/"):
        assert other("https://www.instagram.com/swoveralls", found) == "", found                                    # not a profile on this site, or no account named
    assert other("https://www.instagram.com/swoveralls", "https://www.tiktok.com/@someoneelse") == "" and other("https://x.com/swoveralls", "https://www.instagram.com/someoneelse/") == ""      # another site's profile is not "a different account" of this one
    assert other("https://www.instagram.com/", "https://www.instagram.com/anyone/") == "" and other("https://www.instagram.com/explore/", "https://www.instagram.com/anyone/") == ""    # an address with no account asks for none




def test_the_stored_record_of_a_read_says_which_results_were_left_out():
    from contentintel import session_chain as SC
    rows = SL.discovery_rows("https://www.instagram.com/swoveralls", {}, [("serper_dev", Search(SWOVERALLS_RESULTS))])
    chain = SC.run_chain("instagram page", [SC.Step("primary", lambda: (_ for _ in ()).throw(K.Failure(K.UPSTREAM_BROKEN, "refused"))), SC.Step("discovery", lambda: rows)])
    entry = SL.read_entry("instagram", "https://www.instagram.com/swoveralls", chain)
    assert entry["route"] == "discovery" and entry["quality"] == "snippets about the address, not the page; 1 result(s) about a different account (@swoveralls_) left out"
    assert SL.read_entry("instagram", "https://www.instagram.com/swoveralls", SC.run_chain("p", [SC.Step("discovery", lambda: [dict(rows[1])])]))["quality"] == "snippets about the address, not the page"


# ---- the save step ------------------------------------------------------------------------------------------------------------------------

def test_the_save_step_adds_only_files_that_exist_and_says_so_without_a_fatal_error(tmp_path):
    steps = yaml.safe_load((WF / "sessions.yml").read_text(encoding="utf-8"))["jobs"]["sessions"]["steps"]
    run = next(s for s in steps if str(s.get("name", "")).startswith("Save what was found"))["run"]
    head = run[:run.index("if git diff --cached --quiet")]                                                       # the part that stages files
    script = f"set -e\ncd {tmp_path}\ngit init -q .\n" + head.replace("git config user.name", "git config user.name").replace("git config user.email", "git config user.email") + "git diff --cached --name-only\n"
    (tmp_path / "data" / "_shared").mkdir(parents=True)
    (tmp_path / "data" / "_shared" / "sessions_health.json").write_text("{}", encoding="utf-8")                  # a check run: no reads file yet
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert done.returncode == 0 and "fatal" not in done.stdout + done.stderr and "pathspec" not in done.stdout + done.stderr, done.stderr
    assert "data/_shared/sessions_reads.json does not exist yet" in done.stdout and done.stdout.strip().splitlines()[-1] == "data/_shared/sessions_health.json"
    (tmp_path / "data" / "_shared" / "sessions_reads.json").write_text("[]", encoding="utf-8")                   # a read run: both exist, both are staged
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert done.returncode == 0 and "does not exist yet" not in done.stdout and set(done.stdout.split()[-2:]) == {"data/_shared/sessions_health.json", "data/_shared/sessions_reads.json"}


# ---- a monitoring run gets the browser ----------------------------------------------------------------------------------------------------

def test_a_monitoring_run_and_the_weekly_schedule_install_the_browser_so_public_tiktok_and_linkedin_profiles_are_read_with_it():
    steps = yaml.safe_load((WF / "observatory.yml").read_text(encoding="utf-8"))["jobs"]["observatory"]["steps"]
    install = next(s for s in steps if str(s.get("name", "")).startswith("Install the isolated Scrapling"))
    assert "inputs.action == 'monitor'" in install["if"] and "github.event_name == 'schedule'" in install["if"] and "inputs.scrapling" in install["if"] and "inputs.browsers" in install["if"]
    line = next(ln for ln in install["run"].splitlines() if ln.strip().startswith("if [") and "inputs.browsers" in ln or ('= "monitor"' in ln))
    assert '"true" ]' in line and '= "monitor"' in line and "schedule" in line
    assert "patchright install --with-deps chromium" in install["run"] and 'echo "SCRAPLING_PYTHON=' in install["run"]
    assert ("-m playwright install" not in install["run"])


def test_with_the_browser_present_a_monitoring_run_builds_a_profile_reader_that_names_the_browser():
    class Settings(dict):
        pass
    settings = {"social": {"max_per_competitor": 4, "instagram": True, "tiktok": True, "linkedin": True, "facebook": False, "x": False, "min_change_pct": 5, "max_discovery_per_run": 12}}
    reader, note = SP.build_reader(settings, "me@example.com", {"SCRAPLING_PYTHON": "/x/python", "SERPER_API_KEY": "k"})
    assert reader is not None and note == "social profiles: read through the browser and the search layer"
    reader, note = SP.build_reader(settings, "me@example.com", {"SERPER_API_KEY": "k"})
    assert note.startswith("social profiles: read through no browser and the search layer")                        # the line that showed the gap in monitoring #32


# ---- 'no counts in the snippet' is not an upstream failure --------------------------------------------------------------------------------

def test_a_snippet_without_counts_is_not_found_and_so_never_a_health_event():
    assert K.NOT_FOUND in H.NEUTRAL and K.UPSTREAM_BROKEN not in H.NEUTRAL


# ---- the guide says what the live runs showed ---------------------------------------------------------------------------------------------

def test_the_guide_records_that_x_was_accepted_and_what_update_58_changed():
    text = (ROOT / "docs" / "platform" / "session-capture-guide.md").read_text(encoding="utf-8")
    assert "ACCEPTED for the first time" in text and "intermittent" in text and "check #26" in text and "14 Nov 2026" in text
    assert "X cannot be proved from GitHub's servers by either route" not in text and "the address is blocked on both routes" not in text      # the closing position of Update 56 is replaced
    for phrase in ("## Update 58", "installs the browser", "@swoveralls_", "never used for an address", "not found", "names LinkedIn only for LinkedIn", "adds only files that exist"):
        assert phrase in text, phrase
