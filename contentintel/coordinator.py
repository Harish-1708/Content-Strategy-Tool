"""Claude as the coordinator: it understands the task, picks the lane, reviews specialist output and says what needs a person.

The coordinator never lowers a data class and never sends anything itself; the router does that, under the same rules.
Every decision it makes is checked against the known lanes, and a plain keyword fallback keeps the system working when the
model is unavailable or answers badly.
"""
import json

from . import validate

_KEYWORDS = (
    ("brief", "brief_generation"), ("strategy", "strategic_reasoning"), ("trend", "trend_opportunity"), ("opportunit", "trend_opportunity"),
    ("competitor", "competitor_intelligence"), ("audience", "audience_voice"), ("customer", "audience_voice"), ("hook", "formula_analysis"),
    ("formula", "formula_analysis"), ("idea", "content_ideation"), ("storyboard", "production_support"), ("video", "production_support"),
    ("check", "validation_qa"), ("review", "validation_qa"), ("extract", "research_extraction"), ("research", "research_extraction"),
)


class Coordinator:
    def __init__(self, client, lanes, model="claude"):
        self.client, self.lanes, self.model = client, lanes, model

    def choose_lane(self, task, data_class):
        """(lane, how): the lane for a task in words, chosen by Claude and verified, else by keywords, else None (a person decides)."""
        names = sorted(self.lanes)
        if self.client is not None:
            try:
                reply = self.client.complete({"provider": "claude", "model": self.model, "gateway_model": self.model},
                                             "Pick the one lane that fits the task. Answer only JSON like {\"lane\": \"name\"}.",
                                             f"Lanes: {', '.join(names)}\nTask (data class {data_class}): {task[:800]}", 60)
                lane = validate.extract_json(reply.text).get("lane")
                if lane in self.lanes:
                    return lane, "claude"
            except Exception:                              # the coordinator failing must never stop the platform
                pass
        low = task.lower()
        for word, lane in _KEYWORDS:
            if word in low and lane in self.lanes:
                return lane, "keywords"
        return None, "undecided"

    @staticmethod
    def conflicts(outputs):
        """Fields on which specialist JSON outputs disagree: a deterministic check, so disagreement is never missed or invented."""
        found = {}
        dicts = [o for o in outputs if isinstance(o, dict)]
        for key in sorted({k for d in dicts for k in d}):
            values = {json.dumps(d[key], sort_keys=True) for d in dicts if key in d}
            if len(values) > 1:
                found[key] = sorted(values)
        return found

    @staticmethod
    def needs_person(lane, outputs, route_count):
        """Reasons a person should look: the lane demands it, specialists disagree, or only one weak route answered."""
        reasons = []
        if lane.review == "required":
            reasons.append("this lane requires human review")
        if Coordinator.conflicts(outputs):
            reasons.append("specialist outputs disagree on " + ", ".join(Coordinator.conflicts(outputs)))
        if route_count == 0:
            reasons.append("no route produced a usable answer")
        return reasons
