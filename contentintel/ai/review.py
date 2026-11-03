"""The human-review queue: important AI outputs wait here for a person's decision.

Stored in data/_shared/ai_review.json. The system only ever adds items; a decision is written by a person
(through `ai review`) and is never overwritten by a later run.
"""
import json
from pathlib import Path


class ReviewQueue:
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        except ValueError:
            data = {}
        self.data = data if isinstance(data, dict) and isinstance(data.get("items"), dict) else {"next": 1, "items": {}}
        self.data.setdefault("next", 1)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, *, lane, now, output, route, item_ids, data_class, reason):
        rid = f"AR-{self.data['next']:04d}"
        self.data["next"] += 1
        self.data["items"][rid] = {"id": rid, "created": now, "lane": lane, "reason": reason, "data_class": data_class, "item_ids": list(item_ids),
                                   "route": route, "output": output, "decision": "", "decided_by": "", "decided_at": "", "note": ""}
        return rid

    def pending(self):
        return [i for i in self.data["items"].values() if not i["decision"]]

    def decide(self, rid, decision, who, now, note=""):
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        item = self.data["items"].get(rid)
        if item is None:
            raise KeyError(f"no review item {rid}")
        if item["decision"]:
            raise ValueError(f"{rid} was already {item['decision']} by {item['decided_by']}; a decision is not overwritten")
        item.update({"decision": decision, "decided_by": who, "decided_at": now, "note": note})
        return item
