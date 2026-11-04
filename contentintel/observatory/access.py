"""The gate for advanced access methods.

Scrapling and Agent-Reach carry capabilities beyond ordinary fetching: stealth browsers, challenge handling, browser fingerprints, proxy use,
logged-in browser sessions. They are valuable on sites and accounts we own, or where we hold written authorisation (our own storefronts behind
a bot shield, a client's site, an account we run). Used on someone else's site they would defeat that site's security controls.

So each is a *method* that is off for every host unless the host is listed under `authorised_access` in observatory.yaml with a basis
(owner, client_authorised, written_permission), the person who decided, a date, and the specific methods allowed. The listing is a recorded decision, as
the Internal-data approval is. This module is the only place that decides, it is asked before every use, and the isolated worker asks it again.
"""
from .. import contract as K
from . import gatecheck


class Gate:
    def __init__(self, settings):
        self.entries = {e["host"]: e for e in settings["authorised_access"]["hosts"]}

    def entry_for(self, host):
        return gatecheck.entry_for(list(self.entries.values()), host)

    def allows(self, host, method):
        """(True, the recorded basis) or (False, why not)."""
        return gatecheck.allows(list(self.entries.values()), host, method)

    def require(self, host, *methods):
        """Raise BAD_INPUT unless every method is allowed for the host. Returns the recorded basis, for the evidence record."""
        try:
            return gatecheck.require(list(self.entries.values()), host, *methods)
        except ValueError as exc:
            raise K.Failure(K.BAD_INPUT, str(exc), hint="Advanced access is only for sites and accounts we own or are authorised to use. Add the host to authorised_access in "
                            "observatory.yaml with its basis, who approved it and the date, if that is true.") from None

    def listing(self):
        return [{"host": e["host"], "methods": e["methods"], "basis": e["basis"], "approved_by": e["approved_by"], "date": e["date"]} for e in self.entries.values()]
