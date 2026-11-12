"""The owner's central controls: everything supported is ON by default, any single switch can be turned OFF and back ON, and the shipped observatory.yaml says what the code says."""
import pytest

from contentintel import services as SV

SWITCHES = [c for c in SV.CONTROLS if c["kind"] == "bool" and c["key"].startswith("services.") and ".defaults." not in c["key"] and not c["key"].startswith(("services.guards.", "services.stored_sessions.", "services.connection_secrets.", "services.connection_routes.", "services.connection_versions."))]       # permissions; the .defaults. entries are per-request tuning, below


def test_the_shipped_observatory_yaml_agrees_with_every_default_in_the_code():
    shipped, code = SV.load_settings("observatory.yaml", "no-such-data-dir"), SV.load_settings("missing.yaml", "no-such-data-dir")
    assert shipped["services"] == code["services"]
    for key in ("readers", "discovery"):
        assert shipped[key] == code[key]


def test_every_on_off_control_in_the_registry_is_on_by_default_and_every_route_defaults_to_the_most_capable_choice():
    off = [c["key"] for c in SV.CONTROLS if c["kind"] == "bool" and c["default"] is not True and ".defaults." not in c["key"] and not c["key"].startswith("services.guards.")]      # guards are the one family that starts OFF: see tests/test_guards.py
    assert off == [] and len(SWITCHES) > 70
    assert {c["key"] for c in SV.CONTROLS if c["key"].startswith("access_controls.")} == {f"access_controls.{k}" for k in ("custom_identity", "proxy", "session", "browser", "account_cookies", "evasion")}
    tuning = [c for c in SV.CONTROLS if ".defaults." in c["key"]]
    assert tuning and all(c["default"] is True for c in tuning if c["kind"] == "bool") and all(c["default"] in (0, "") for c in tuning if c["kind"] != "bool")          # every on/off request option is ON, including solve_challenges; the numbers and texts are unset
    routes = {c["key"]: c["default"] for c in SV.CONTROLS if c["kind"] == "choice" and c["key"].startswith("services.")}
    expected = lambda k: "gateway_first" if ".ai_gateway." in k else "external_first" if ".connection_routes." in k else "latest" if ".connection_versions." in k else "auto"      # noqa: E731
    assert routes and all(v == expected(k) for k, v in routes.items())
    s = SV.load_settings("observatory.yaml", "no-such-data-dir")["services"]
    assert s["scrapling"]["enabled"] and s["agent_reach"]["enabled"] and s["search"]["enabled"] and s["ai_gateway"]["enabled"] and s["page_readers"]["enabled"]
    assert all(s["agent_reach"]["channels"].values()) and all(s["search"]["channels"].values()) and s["search"]["channels"]["serper_dev"]       # Google search (Serper) included


def _allowed_for(settings, key):
    """Ask the shared service the question the key answers; returns (the switch itself, a sibling that must be unaffected)."""
    parts = key.split(".")
    service, kind = parts[1], parts[2]
    if kind == "enabled":
        return SV.allowed(settings, service, "brief"), None
    if kind == "use_for":
        sibling = "planning" if parts[3] != "planning" else "brief"
        return SV.allowed(settings, service, parts[3]), SV.allowed(settings, service, sibling)
    channel = parts[3]
    other = next(c for c in settings["services"][service]["channels"] if c != channel)
    return SV.allowed(settings, service, "brief", channel=channel), SV.allowed(settings, service, "brief", channel=other)


@pytest.mark.parametrize("key", [c["key"] for c in SWITCHES])
def test_turning_any_single_switch_off_blocks_exactly_that_and_turning_it_back_on_restores_it(key, tmp_path):
    data = str(tmp_path)
    on, sibling_on = _allowed_for(SV.load_settings("observatory.yaml", data), key)
    assert on[0] is True
    SV.save_overrides(data, {key: False}, "owner", "2026-11-04T10:00:00Z", "off")
    off, sibling = _allowed_for(SV.load_settings("observatory.yaml", data), key)
    assert off[0] is False and key.split(".", 1)[1] in off[1] or key in off[1]                 # refused, and the message names the switch
    if sibling_on is not None:
        assert sibling[0] is True                                                              # nothing else was affected
    SV.save_overrides(data, {key: True}, "owner", "2026-11-04T10:01:00Z", "on")
    again, _ = _allowed_for(SV.load_settings("observatory.yaml", data), key)
    assert again[0] is True


@pytest.mark.parametrize("service", ["ai_gateway", "page_readers"])
def test_each_route_can_be_chosen_per_purpose_and_reset_to_its_default(service, tmp_path):
    data = str(tmp_path)
    route = [c for c in SV.CONTROLS if c["kind"] == "choice" and c["key"].startswith(f"services.{service}.routes.")][0]
    other = [x for x in route["choices"] if x != route["default"]][0]
    SV.save_overrides(data, {route["key"]: other}, "owner", "2026-11-04T10:00:00Z", "chose")
    purpose = route["key"].rsplit(".", 1)[1]
    assert SV.route_for(SV.load_settings("observatory.yaml", data), service, purpose) == other
    SV.save_overrides(data, {}, "owner", "2026-11-04T10:01:00Z", "reset")
    assert SV.route_for(SV.load_settings("observatory.yaml", data), service, purpose) == route["default"]
