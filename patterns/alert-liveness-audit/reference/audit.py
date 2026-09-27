#!/usr/bin/env python3
"""Alert liveness audit -- find rules that look armed and cannot fire.

Read-only. Walks every alerting rule on a Grafana stack and reports the failures that are
visible WITHOUT reading the rule as a human would:

    PENDING_NEVER_ALERTING   history has Pending transitions and no Alerting ones: the pending
                             period is longer than the signal stays visible
    NODATA_ONLY              every history entry is a NoData state: the signal does not exist
    NO_HISTORY               nothing recorded in the window: the rule has never been asked
    EVALUATOR_ON_REDUCE      the condition points at a reduce/math node that carries an
                             evaluator, which that node type ignores
    FOR_EXCEEDS_WINDOW       the pending period is at least the range window: only correct
                             if the signal is continuous, so check its cadence
    NODATA_OK_WITH_VECTOR0   no-data treated as OK and `or vector(0)` in the expression: a
                             total outage renders as zero bad events

Environment:
    GRAFANA_URL     e.g. https://your-stack.grafana.net
    GRAFANA_TOKEN   a service-account token with read access to alerting

The script refuses to exit green unless it audited at least one rule AND read at least one
history entry. A clean report over nothing is the failure this whole pattern is about.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

RANGE_RE = re.compile(r"\[(\d+)([smhd])\]")
UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass
class Finding:
    kind: str
    uid: str
    title: str
    detail: str

    def line(self) -> str:
        return f"{self.kind:24} {self.uid:16} {self.title[:36]!r:38} {self.detail}"


# --------------------------------------------------------------------------- parsing helpers

def seconds(duration: str | None) -> int:
    """'5m' -> 300. Accepts the compound forms Grafana emits ('1h30m')."""
    if not duration:
        return 0
    total, num = 0, ""
    for ch in duration:
        if ch.isdigit():
            num += ch
        elif ch in UNIT_S and num:
            total += int(num) * UNIT_S[ch]
            num = ""
    return total


def smallest_window(rule: dict) -> int | None:
    """The shortest range window in any data-query expression, in seconds, or None."""
    windows = []
    for node in rule.get("data", []):
        expr = (node.get("model") or {}).get("expr") or ""
        windows += [int(n) * UNIT_S[u] for n, u in RANGE_RE.findall(expr)]
    return min(windows) if windows else None


def uses_vector0(rule: dict) -> bool:
    return any(
        re.search(r"\bor\s+vector\(0\)", (n.get("model") or {}).get("expr") or "")
        for n in rule.get("data", [])
    )


# --------------------------------------------------------------------------- static checks

def check_definition(rule: dict) -> list[Finding]:
    """The dead-config shapes that CAN be seen in the definition. Most cannot."""
    uid, title = rule.get("uid", "?"), rule.get("title", "?")
    out: list[Finding] = []
    nodes = {n.get("refId"): n for n in rule.get("data", [])}
    cond = nodes.get(rule.get("condition"))

    if cond is not None:
        model = cond.get("model") or {}
        if model.get("type") in ("reduce", "math"):
            params = [c.get("evaluator", {}).get("params") for c in model.get("conditions", [])]
            armed = [p for p in params if p]
            out.append(Finding(
                "EVALUATOR_ON_REDUCE", uid, title,
                f"condition {rule.get('condition')} is {model.get('type')}; "
                + (f"evaluator {armed[0]} is ignored" if armed else "any non-zero value fires"),
            ))

    window = smallest_window(rule)
    pending = seconds(rule.get("for"))
    if window and pending and pending >= window:
        out.append(Finding(
            "FOR_EXCEEDS_WINDOW", uid, title,
            f"for={rule.get('for')}, window={window // 60}m: check the signal's cadence",
        ))

    if rule.get("noDataState") == "OK" and uses_vector0(rule):
        out.append(Finding(
            "NODATA_OK_WITH_VECTOR0", uid, title,
            "no-data is OK and the expression has `or vector(0)`: an outage renders as 0",
        ))
    return out


# --------------------------------------------------------------------------- history checks

def classify_history(uid: str, title: str, states: list[str]) -> Finding | None:
    """`states` is the sequence of `current` values from state history, oldest first."""
    if not states:
        return Finding("NO_HISTORY", uid, title, "no state transitions in the window")
    if all("NoData" in s for s in states):
        return Finding("NODATA_ONLY", uid, title, f"{len(states)} transitions, all NoData")
    pending = sum(s.startswith("Pending") for s in states)
    alerting = sum(s.startswith("Alerting") for s in states)
    if pending and not alerting:
        return Finding("PENDING_NEVER_ALERTING", uid, title,
                       f"{pending} pending, 0 alerting in the window")
    return None


# --------------------------------------------------------------------------- Grafana API

class Grafana:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def get(self, path: str, **params) -> dict:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(
            self.url + path + query, headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def rules(self) -> list[dict]:
        """Provisioned definitions: condition, data, for, no-data state."""
        return self.get("/api/v1/provisioning/alert-rules")

    def history_states(self, uid: str, since_s: int, until_s: int) -> list[str]:
        """The `current` state of each transition, oldest first."""
        # `from` is a Python keyword, hence the dict rather than keyword arguments.
        body = self.get("/api/v1/rules/history",
                        **{"ruleUID": uid, "from": since_s, "to": until_s, "limit": 1000})
        values = (body.get("data") or {}).get("values") or []
        if len(values) < 2:
            return []
        stamped = sorted(zip(values[0], values[1]), key=lambda t: t[0])
        return [str(line.get("current", "")) for _, line in stamped]


# --------------------------------------------------------------------------- entrypoint

def audit(client: Grafana, days: int, skip_paused: bool = True) -> tuple[list[Finding], int, int]:
    """Returns (findings, rules audited, history entries read)."""
    until = int(time.time())
    since = until - days * 86400
    findings: list[Finding] = []
    audited = entries = 0

    for rule in client.rules():
        if skip_paused and rule.get("isPaused"):
            continue
        audited += 1
        findings += check_definition(rule)
        states = client.history_states(rule["uid"], since, until)
        entries += len(states)
        hit = classify_history(rule["uid"], rule.get("title", "?"), states)
        if hit:
            findings.append(hit)
    return findings, audited, entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14, help="history window (default 14)")
    ap.add_argument("--include-paused", action="store_true")
    args = ap.parse_args(argv)

    url, token = os.environ.get("GRAFANA_URL"), os.environ.get("GRAFANA_TOKEN")
    if not url or not token:
        print("GRAFANA_URL and GRAFANA_TOKEN are required", file=sys.stderr)
        return 2

    findings, audited, entries = audit(Grafana(url, token), args.days, not args.include_paused)

    for f in sorted(findings, key=lambda f: (f.kind, f.title)):
        print(f.line())
    print(f"audited {audited} rules, {entries} history entries: {len(findings)} findings")

    # The positive control. A clean report over nothing is not a clean report.
    if audited == 0 or entries == 0:
        print("REFUSING a green result: audited no rules or read no history. "
              "Check the token's organisation and whether state history is enabled.", file=sys.stderr)
        return 3
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
