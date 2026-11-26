#!/usr/bin/env python3
"""Resolve a rebase conflict in ONE json data file by merging the two runs' data, not by choosing one of them.
Used by push-with-retry.sh while a `git pull --rebase` is stopped on a conflict. Stage 1 is the common ancestor, stage 2 the other run's saved version (already on the branch), stage 3 this run's version.
  * objects: every key either run added is kept; a key only one run changed takes that run's value; a key both changed is merged the same way (objects, lists) or, for plain values, takes THIS run's value (the newer result);
  * lists: the other run's items first, then this run's items that are not already there (an item is the same when its JSON is the same);
  * a key this run deleted stays deleted unless the other run changed it.
Usage: merge-json-conflict.py <path>   (writes the merged file; exits 1 when a version is not valid JSON, and the caller keeps this run's version)."""
import json
import subprocess
import sys


def stage(path, n):
    out = subprocess.run(["git", "show", f":{n}:{path}"], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None


def merge(base, other, mine):
    if other == mine:
        return mine
    if other == base:
        return mine
    if mine == base:
        return other
    if isinstance(other, dict) and isinstance(mine, dict):
        base = base if isinstance(base, dict) else {}
        out = {}
        for key in list(other) + [k for k in mine if k not in other]:
            in_o, in_m, in_b = key in other, key in mine, key in base
            if in_o and in_m:
                out[key] = merge(base.get(key), other[key], mine[key])
            elif in_o:
                if not (in_b and base[key] == other[key]):                      # I deleted a key the other run left alone: it stays deleted
                    out[key] = other[key]
            elif not (in_b and base[key] == mine[key]):                          # the other run deleted a key I left alone: it stays deleted
                out[key] = mine[key]
        return out
    if isinstance(other, list) and isinstance(mine, list):
        seen = {json.dumps(x, sort_keys=True) for x in other}
        return list(other) + [x for x in mine if json.dumps(x, sort_keys=True) not in seen]
    return mine


def main(path):
    try:
        merged = merge(stage(path, 1), stage(path, 2), stage(path, 3))
    except ValueError:
        return 1
    if merged is None:
        return 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
