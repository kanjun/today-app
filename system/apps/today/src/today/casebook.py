"""Mistakes the user has corrected, kept as cases and re-run whenever Today's reading changes.

Each case is a snapshot of one item and its whole conversation as it was when the reading
went wrong, plus what the right reading is. ``run`` sends every case through the current
reader (the same call sync makes) and reports which still come out right. Nothing is
written to the user's items; the reader only reads (their mail, for its searches).

    uv run python -m today.casebook                 # re-check every case
    uv run python -m today.casebook add NAME KEY --why "..." --expect '[...]'

Expectations, each a JSON object:
    {"field": "state", "one_of": ["ready", "you", "mine"]}
    {"field": "resolved", "is": false}
    {"field": "due", "between": ["2026-11-05", "2026-11-12"]}
    {"promise": {"asked": "Sam", "quote_has": "Priya"}}         a promise like this is found
    {"no_promise": {"asked": "", "quote_has": "Priya"}}         and none like this
"""

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from today import ai, store, work

log = logging.getLogger(__name__)

# What the reader is given about an item; everything else on it is the reader's own output.
INPUT_FIELDS = (
    "key",
    "kind",
    "source",
    "account",
    "url",
    "ref",
    "who",
    "asked_at",
    "ask_id",
    "ask_text",
    "manual",
    "no",
)


def cases_dir() -> Path:
    return store.DATA_DIR / "cases"


def add(name: str, key: str, why: str, expect: list[dict[str, Any]]) -> Path:
    """Snapshot the item and its conversation as a case."""
    item = store.load()["items"][key]
    case = {
        "name": name,
        "why": why,
        "added": store.now_iso(),
        "item": {k: item[k] for k in INPUT_FIELDS if k in item},
        "raw": store.load_raw(key) or {},
        "expect": expect,
    }
    path = cases_dir() / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=1))
    return path


def _matches(promise: dict[str, Any], like: dict[str, str]) -> bool:
    asked = str(promise.get("asked") or "").lower()
    if "asked" in like and asked != like["asked"].lower():
        return False
    text = f"{promise.get('title', '')} {promise.get('quote', '')}".lower()
    return like.get("quote_has", "").lower() in text


def check(answer: dict[str, Any], expect: list[dict[str, Any]]) -> list[str]:
    """What in the answer is wrong, one plain line each; empty when it is all right."""
    wrong = []
    for e in expect:
        if "promise" in e:
            if not any(_matches(p, e["promise"]) for p in answer.get("promises") or []):
                wrong.append(f"no promise like {e['promise']}")
            continue
        if "no_promise" in e:
            bad = [
                p for p in answer.get("promises") or [] if _matches(p, e["no_promise"])
            ]
            if bad:
                wrong.append(f"found a promise it should not: {bad[0]}")
            continue
        got = answer.get(e["field"])
        if "is" in e and got != e["is"]:
            wrong.append(f"{e['field']} is {got!r}, should be {e['is']!r}")
        if "one_of" in e and got not in e["one_of"]:
            wrong.append(f"{e['field']} is {got!r}, should be one of {e['one_of']}")
        if "between" in e and not (
            e["between"][0] <= str(got or "") <= e["between"][1]
        ):
            wrong.append(f"{e['field']} is {got!r}, should be in {e['between']}")
    return wrong


def run_case(case: dict[str, Any]) -> list[str]:
    try:
        answer = work.take_it_further(dict(case["item"]), case["raw"])
    except (ai.ClaudeError, OSError, ValueError, KeyError) as e:
        return [f"the reader failed: {str(e)[:200]}"]
    return check(answer, case["expect"])


def run(names: list[str] | None = None) -> dict[str, list[str]]:
    cases = [json.loads(p.read_text()) for p in sorted(cases_dir().glob("*.json"))]
    cases = [c for c in cases if not names or c["name"] in names]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run_case, cases))
    return {c["name"]: r for c, r in zip(cases, results)}


def main() -> int:
    parser = argparse.ArgumentParser(prog="today.casebook")
    sub = parser.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("key")
    a.add_argument("--why", required=True)
    a.add_argument("--expect", required=True)
    r = sub.add_parser("run")
    r.add_argument("names", nargs="*")
    args = parser.parse_args()
    if args.cmd == "add":
        log.info(
            "saved %s", add(args.name, args.key, args.why, json.loads(args.expect))
        )
        return 0
    results = run(getattr(args, "names", None) or None)
    for name, wrong in results.items():
        log.info(
            "%s %s%s",
            "ok  " if not wrong else "FAIL",
            name,
            "".join(f"\n     - {w}" for w in wrong),
        )
    failed = sum(1 for w in results.values() if w)
    log.info("%d of %d cases right", len(results) - failed, len(results))
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
