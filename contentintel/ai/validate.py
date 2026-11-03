"""Checks on what a model returned, before anyone relies on it: valid JSON, the right shape, usable text, no forbidden claims.

No output reaches a brief, a score or a decision without passing these. They do not prove an answer is true; they catch
answers that are empty, malformed, refused, truncated or that state something the lane forbids.
"""
import json
import re

_REFUSAL = re.compile(r"\b(i can(?:'|’)?t (?:help|assist|comply)|i cannot (?:help|assist|comply)|i(?:'|’)m sorry,? but|as an ai (?:language )?model)\b", re.I)


class OutputProblem(Exception):
    """The output cannot be used. The message says why, in plain words."""


def extract_json(text):
    body = (text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
    if fenced:
        body = fenced.group(1)
    for candidate in (body, body[body.find("{"):body.rfind("}") + 1], body[body.find("["):body.rfind("]") + 1]):
        if candidate:
            try:
                return json.loads(candidate)
            except ValueError:
                continue
    raise OutputProblem("the answer is not valid JSON")


_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}


def check_schema(value, schema, path="answer"):
    """Problems (a list of plain sentences) with value against a small JSON-Schema subset: type, required, properties, enum, items, lengths, ranges."""
    problems = []
    t = schema.get("type")
    if t:
        ok = isinstance(value, _TYPES[t]) and not (t in ("number", "integer") and isinstance(value, bool))
        if not ok:
            return [f"{path} should be {t}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path} is shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path} is longer than {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path} is below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path} is above {schema['maximum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path} is missing '{key}'")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problems += check_schema(value[key], sub, f"{path}.{key}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path} has fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path} has more than {schema['maxItems']} items")
        if "items" in schema:
            for i, v in enumerate(value[:50]):
                problems += check_schema(v, schema["items"], f"{path}[{i}]")
    return problems


def check_output(text, lane):
    """The parsed answer (a dict or list for JSON lanes, text otherwise), or raise OutputProblem."""
    if not (text or "").strip():
        raise OutputProblem("the answer is empty")
    if _REFUSAL.search(text[:300]) and len(text) < 600:
        raise OutputProblem("the model refused or deflected")
    low = text.lower()
    for phrase in lane.forbid:
        if phrase.lower() in low:
            raise OutputProblem(f"the answer contains a forbidden phrase ({phrase!r})")
    spec = lane.output
    if spec["format"] == "json":
        value = extract_json(text)
        problems = check_schema(value, spec.get("schema") or {})
        if problems:
            raise OutputProblem("; ".join(problems[:3]))
        if lane.min_confidence and isinstance(value, dict) and "confidence" in value:
            if value["confidence"] < lane.min_confidence:
                raise OutputProblem(f"the model's own confidence ({value['confidence']}) is below the lane's minimum ({lane.min_confidence})")
        return value
    if len(text.strip()) < int(spec.get("min_chars", 1)):
        raise OutputProblem(f"the answer is shorter than {spec.get('min_chars')} characters")
    return text.strip()
