#!/usr/bin/env python3
"""Alert liveness audit -- find rules that look armed and cannot fire.

Read-only. Walks every alerting rule on a Grafana stack and reports what can be seen WITHOUT
reading each rule as a human would. Two tiers, because the first run on a real estate taught
the difference: a finding is a rule that needs a decision; a review item is a rule the audit
cannot vouch for and cannot condemn.

Findings (exit 1):
    PENDING_NEVER_ALERTING  reached Pending in the window and never Alerting. Either the pending
                            period is doing its job or it is longer than the signal stays
                            visible; the episode lengths printed alongside are how to tell.
    NODATA_ONLY             every transition in the window is a NoData state: the signal is absent
    DEAD_EVALUATOR          the condition points at a reduce/math node that carries a real
                            threshold, which that node type ignores (any non-zero value fires)
    FOR_ON_SINGLE_EVENT     a "> 0" rule over a windowed count of events with a pending period at
                            least the window: one event can never fire it

Review (printed, exit 0):
    EVALUATOR_ON_REDUCE     as DEAD_EVALUATOR but the evaluator is empty or zero, so "any
                            non-zero" was probably the intent -- confirm it
    FOR_EXCEEDS_WINDOW      pending period >= window on a continuous signal: fine while the
                            signal is continuous, dead the day it becomes sparse
    TOO_MANY_NO_LIVENESS    a "> N" rule with `or vector(0)` and no-data treated as OK: a
                            telemetry outage renders as zero, so it needs a paired absence rule
    YOUNG                   changed inside the window, so its history is shorter than the window
    QUIET                   no transitions in the window: history cannot vouch for it either way

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
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

RANGE_RE = re.compile(r"\[(\d+)([smhd])\]")
# Windowed counts of discrete events. `increase()` and `changes()` are deliberately NOT here:
# over a continuously scraped counter they are rate-shaped, and the standard kube-prometheus
# rules pair them with a pending period longer than the window on purpose.
EVENT_FN_RE = re.compile(r"\b(count_over_time|sum_over_time)\(")
VECTOR0_RE = re.compile(r"\bor\s+vector\(0\)")
UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}
FINDING_KINDS = {"PENDING_NEVER_ALERTING", "NODATA_ONLY", "DEAD_EVALUATOR", "FOR_ON_SINGLE_EVENT"}
TRIVIAL_EVALUATORS = ([], [0], [0, 0])


@dataclass
class Finding:
    kind: str
    uid: str
    title: str
    detail: str

    @property
    def is_finding(self) -> bool:
        return self.kind in FINDING_KINDS

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


def human(secs: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs / size:.0f}{unit}"
    return f"{secs:.0f}s"


def expressions(rule: dict) -> str:
    return " ".join((n.get("model") or {}).get("expr") or "" for n in rule.get("data", []))


def smallest_window(rule: dict) -> int | None:
    """The shortest range window in any data-query expression, in seconds, or None."""
    windows = [int(n) * UNIT_S[u] for n, u in RANGE_RE.findall(expressions(rule))]
    return min(windows) if windows else None


def is_log_datasource(rule: dict, log_uids: set[str]) -> bool:
    return any(n.get("datasourceUid") in log_uids for n in rule.get("data", []))


def condition_node(rule: dict) -> dict:
    return next((n for n in rule.get("data", []) if n.get("refId") == rule.get("condition")), {})


def evaluator(rule: dict) -> tuple[str, list]:
    """(type, params) of the condition node's evaluator, or ('', [])."""
    conds = (condition_node(rule).get("model") or {}).get("conditions") or []
    ev = (conds[0].get("evaluator") or {}) if conds else {}
    return ev.get("type", ""), list(ev.get("params") or [])


def updated_within(rule: dict, since_s: int) -> bool:
    stamp = rule.get("updated") or ""
    try:
        y, m, d = int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10])
    except ValueError:
        return False
    return time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)) >= since_s


# --------------------------------------------------------------------------- static checks

def check_definition(rule: dict, log_uids: set[str] = frozenset()) -> list[Finding]:
    """The dead-config shapes that CAN be seen in the definition. Most cannot."""
    uid, title = rule.get("uid", "?"), rule.get("title", "?")
    out: list[Finding] = []
    node_type = (condition_node(rule).get("model") or {}).get("type")
    ev_type, ev_params = evaluator(rule)
    exprs = expressions(rule)
    window = smallest_window(rule)
    pending = seconds(rule.get("for"))
    single_event = ev_type == "gt" and (not ev_params or ev_params[0] == 0)
    event_shaped = is_log_datasource(rule, log_uids) or bool(EVENT_FN_RE.search(exprs))

    if node_type in ("reduce", "math"):
        if ev_params in TRIVIAL_EVALUATORS:
            out.append(Finding("EVALUATOR_ON_REDUCE", uid, title,
                               f"condition {rule.get('condition')} is {node_type}: fires on any "
                               f"non-zero value; confirm that is the intent"))
        else:
            out.append(Finding("DEAD_EVALUATOR", uid, title,
                               f"condition {rule.get('condition')} is {node_type}; evaluator "
                               f"{ev_type} {ev_params} is ignored, any non-zero value fires"))
        single_event = True   # a reduce condition fires on any non-zero: one event is enough

    if window and pending and pending >= window:
        if single_event and event_shaped:
            out.append(Finding("FOR_ON_SINGLE_EVENT", uid, title,
                               f"for={rule.get('for')} on a {human(window)} count of events with a "
                               f"> 0 threshold: one event cannot fire it"))
        else:
            out.append(Finding("FOR_EXCEEDS_WINDOW", uid, title,
                               f"for={rule.get('for')}, window={human(window)}: fine while the "
                               f"signal is continuous; check its cadence"))

    if rule.get("noDataState") == "OK" and VECTOR0_RE.search(exprs) and ev_type in ("gt", "gte"):
        out.append(Finding("TOO_MANY_NO_LIVENESS", uid, title,
                           "\"> N\" with `or vector(0)` and no-data OK: an outage renders as 0; "
                           "pair it with an absence rule on the same stream"))
    return out


# --------------------------------------------------------------------------- history checks

def pending_episodes(history: list[tuple[int, str, str]], until_s: int | None) -> list[int]:
    """Length of every Pending episode, measured PER INSTANCE.

    History entries are per alert instance (one per label set). A rule over twenty nodes
    interleaves twenty instances' transitions, so "the next entry" is usually another
    instance's, and a global scan reports episodes of zero seconds. Group first.
    """
    by_instance: dict[str, list[tuple[int, str]]] = {}
    for t, s, key in history:
        by_instance.setdefault(key, []).append((t, s))
    episodes = []
    for entries in by_instance.values():
        for i, (t, s) in enumerate(entries):
            if s.startswith("Pending"):
                end = entries[i + 1][0] if i + 1 < len(entries) else (until_s or t)
                episodes.append(max(end - t, 0))
    return episodes


def classify_history(uid: str, title: str, history: list[tuple[int, str, str]],
                     pending_s: int = 0, until_s: int | None = None,
                     young: bool = False) -> Finding | None:
    """`history` is [(unix_seconds, current_state, instance_key)] oldest first."""
    if not history:
        return Finding("YOUNG" if young else "QUIET", uid, title,
                       "changed inside the window; history is shorter than the window"
                       if young else "no state transitions in the window")
    states = [s for _, s, _ in history]
    if all("NoData" in s for s in states):
        return Finding("YOUNG" if young else "NODATA_ONLY", uid, title,
                       f"{len(states)} transitions, all NoData"
                       + ("; rule changed inside the window" if young else ""))
    if any(s.startswith("Alerting") for s in states):
        return None
    episodes = pending_episodes(history, until_s)
    if not episodes:
        return None
    instances = len({key for _, _, key in history})
    longest, median = max(episodes), statistics.median(episodes)
    shape = (f"longest {human(longest)}, median {human(median)} of for={human(pending_s)}"
             f" ({100 * longest / pending_s:.0f}%)" if pending_s else f"longest {human(longest)}")
    return Finding("PENDING_NEVER_ALERTING", uid, title,
                   f"{len(episodes)} pending episode{'s' if len(episodes) != 1 else ''} over "
                   f"{instances} instance{'s' if instances != 1 else ''}, 0 alerting; {shape}")


# --------------------------------------------------------------------------- Grafana API

class Grafana:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def get(self, path: str, **params) -> dict | list:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(
            self.url + path + query, headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def rules(self) -> list[dict]:
        """Provisioned definitions: condition, data, for, no-data state, updated."""
        return [r for r in self.get("/api/v1/provisioning/alert-rules") if not r.get("record")]

    def log_datasource_uids(self) -> set[str]:
        return {d["uid"] for d in self.get("/api/datasources") if d.get("type") == "loki"}

    def history(self, uid: str, since_s: int, until_s: int) -> list[tuple[int, str, str]]:
        """[(unix_seconds, current_state, instance_key)] oldest first."""
        # `from` is a Python keyword, hence the dict rather than keyword arguments.
        body = self.get("/api/v1/rules/history",
                        **{"ruleUID": uid, "from": since_s, "to": until_s, "limit": 5000})
        values = (body.get("data") or {}).get("values") or []
        if len(values) < 2:
            return []
        stamped = sorted(zip(values[0], values[1]), key=lambda t: t[0])
        return [(int(ts) // 1000, str(line.get("current", "")),
                 json.dumps(line.get("labels") or {}, sort_keys=True)) for ts, line in stamped]


# --------------------------------------------------------------------------- entrypoint

def audit(client, days: int, skip_paused: bool = True) -> tuple[list[Finding], int, int]:
    """Returns (findings and review items, rules audited, history entries read)."""
    until = int(time.time())
    since = until - days * 86400
    out: list[Finding] = []
    audited = entries = 0
    log_uids = client.log_datasource_uids()

    for rule in client.rules():
        if rule.get("record"):
            continue    # a recording rule has no alert state; counting it as dead was noise
        if skip_paused and rule.get("isPaused"):
            continue
        audited += 1
        out += check_definition(rule, log_uids)
        history = client.history(rule["uid"], since, until)
        entries += len(history)
        hit = classify_history(rule["uid"], rule.get("title", "?"), history,
                               seconds(rule.get("for")), until, updated_within(rule, since))
        if hit:
            out.append(hit)
    return out, audited, entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14, help="history window (default 14)")
    ap.add_argument("--include-paused", action="store_true")
    ap.add_argument("--show-quiet", action="store_true",
                    help="list QUIET rules individually instead of counting them")
    args = ap.parse_args(argv)

    url, token = os.environ.get("GRAFANA_URL"), os.environ.get("GRAFANA_TOKEN")
    if not url or not token:
        print("GRAFANA_URL and GRAFANA_TOKEN are required", file=sys.stderr)
        return 2

    items, audited, entries = audit(Grafana(url, token), args.days, not args.include_paused)
    findings = [f for f in items if f.is_finding]
    review = [f for f in items if not f.is_finding]

    for f in sorted(findings, key=lambda f: (f.kind, f.title)):
        print(f.line())
    if findings:
        print()
    for f in sorted(review, key=lambda f: (f.kind, f.title)):
        if f.kind != "QUIET" or args.show_quiet:
            print(f.line())
    quiet = sum(f.kind == "QUIET" for f in review)
    if quiet and not args.show_quiet:
        print(f"QUIET                    {quiet} rules had no transitions in {args.days}d; "
              f"history cannot vouch for them (--show-quiet to list)")

    counts = {}
    for f in items:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print(f"\naudited {audited} rules, {entries} history entries: {len(findings)} findings, "
          f"{len(review)} to review ({summary})")

    # The positive control. A clean report over nothing is not a clean report.
    if audited == 0 or entries == 0:
        print("REFUSING a green result: audited no rules or read no history. "
              "Check the token's organisation and whether state history is enabled.", file=sys.stderr)
        return 3
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
