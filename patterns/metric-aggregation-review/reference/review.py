#!/usr/bin/env python3
"""Aggregation recommendation review -- judge a metrics cost engine's queue by its consumers.

Read-only. Reads the verbose recommendation export of Grafana Cloud Adaptive Metrics
(GET /aggregations/recommendations?verbose=true), optionally the live rules
(GET /aggregations/rules) and the dashboards and alert rules that read the metrics, and prints
one verdict per row that needs one. The first line is the queue's net direction: a queue
labelled "recommendations" is not thereby labelled "savings".

Checks, each a pure function; the code is the first column of the output:
    REMOVE_QUERY_ONLY    a remove with no dashboard and no rule behind it, only queries -> reject
    REMOVE_HAS_CONSUMER  a dashboard or rule reads it -> review: the current rule may already
                         serve it
    REMOVE_GONE          the metric has no series now -> apply, housekeeping
    DROPS_PROTECTED      a protected label (default cluster) would be dropped -> modify: keep it
    COUNT_ONLY           count alone proposed on a non-_info metric -> reject; count stores the
                         number of series
    MAX_AS_SUM           a _max metric proposed with sum/count -> modify: only max is valid
    LIVE_RULE_SHAPE      either defect in a rule the engine says to keep -> modify the live rule
    PAIR_SPLIT           _sum/_count/_bucket siblings would end with different drop sets -> mirror
    PAIR_LONE            one side ruled or recommended, its sibling in no input -> review, listed
                         under "not recommended by the engine": the two largest savings were there
    RESTORES_LABELS      an update that only restores labels no consumer asked for -> reject
    RESTORES_AND_DROPS   an update that restores some labels and drops others -> take the drops

Usage counts are evidence of a query, not of a consumer: the export does not say who queried,
investigations and agents count, and the counts age out. Dashboards and alert rules are
consumers; pass them with --consumers so the judgement rests on files you can read.

Exit 0 nothing to act on, 1 verdicts printed, 2 usage or an unreadable input, 3 refused. An
empty queue, or a result without verdicts over a queue reviewed without consumer input, is
refused out loud, never printed as "0 findings".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass

ACTIONS = ("remove", "update", "add", "keep")
PAIR_SUFFIXES = ("_sum", "_count", "_bucket")
EXPR_KEYS = ("expr", "e", "expression")
HISTOGRAM_FN_RE = re.compile(r"\bhistogram_\w+\s*\(")
IDENT_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`')
VARIABLE_RE = re.compile(r"\$\{[^}]*\}|\$\w+")     # Grafana ${var}, $var and math-node $A
LOGQL_RE = re.compile(r"\|[=~]|\|\s*(?:json|logfmt|pattern|regexp|unpack|unwrap|line_format"
                      r"|label_format|drop|keep|decolorize)\b")
MODIFIER_RE = re.compile(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
PROMQL_WORDS = frozenset("by without on ignoring group_left group_right bool offset at and or "
                         "unless inf nan Inf NaN".split())
CONSUMED = ("file", "rule", "dashboard")


@dataclass
class Verdict:
    code: str
    metric: str
    delta: int | None      # series if applied as recommended; None when the export has no counts
    verdict: str           # reject | modify | apply | review
    detail: str

    def line(self) -> str:
        d = "     ?" if self.delta is None else f"{self.delta:+6d}"
        return f"{self.code:19} {self.metric:44} {d}  {self.verdict:7} {self.detail}"


def verdict(code: str, item: dict, tier: str, detail: str) -> Verdict:
    return Verdict(code, item.get("metric", "?"), delta(item), tier, detail)


# --------------------------------------------------------------------------- the export's numbers
def delta(item: dict) -> int | None:
    """Series change if applied: recommended minus current, the engine's own per-row estimate.
    For a remove the recommended count is the raw count. No counts (a metric with no series
    left) is None."""
    for after, before in (("recommended_series_count", "current_series_count"),
                          ("total_series_after_aggregation", "total_series_before_aggregation")):
        if item.get(after) is not None and item.get(before) is not None:
            return int(item[after]) - int(item[before])
    return None


def series_now(item: dict) -> int | None:
    for key in ("raw_series_count", "current_series_count", "total_series_before_aggregation"):
        if item.get(key) is not None:
            return int(item[key])
    return None


def direction(items: list[dict]) -> dict[str, tuple[int, int]]:
    """{action: (rows, series delta)} plus 'total': the one number the console never shows."""
    out: dict[str, list[int]] = {a: [0, 0] for a in ACTIONS}
    for it in items:
        row = out.setdefault(it.get("recommended_action", "?"), [0, 0])
        row[0] += 1
        row[1] += delta(it) or 0
    out["total"] = [sum(v[0] for v in out.values()), sum(v[1] for v in out.values())]
    return {k: (v[0], v[1]) for k, v in out.items()}


# --------------------------------------------------------------------------- consumers
def expressions(node, top: bool = True) -> list[str]:
    """Every expression in a JSON document: 'expr' values (dashboards, alert rules), 'e' values
    (a flat export of rule expressions), or a top-level list of strings."""
    if isinstance(node, list):
        if top and node and all(isinstance(x, str) for x in node):
            return list(node)
        return [e for x in node for e in expressions(x, False)]
    if not isinstance(node, dict):
        return []
    out: list[str] = []
    for key, value in node.items():
        if key in EXPR_KEYS and isinstance(value, (str, list)):
            out += [value] if isinstance(value, str) else [x for x in value if isinstance(x, str)]
        else:
            out += expressions(value, False)
    return out


def family_base(metric: str) -> str | None:
    return next((metric[: -len(s)] for s in PAIR_SUFFIXES if metric.endswith(s)), None)


def metric_names(expr: str) -> set[str]:
    """Metric names a PromQL expression reads. Strings, template variables, label matchers,
    durations, numbers and by/on clauses go first; an identifier followed by '(' is a function.
    A LogQL expression (a pipe outside strings) names no metric, nor does a Grafana math node
    over $A-style references. With a histogram function present a _bucket/_sum/_count name
    expands to its family. A metric selected via __name__=~ is not seen."""
    text = VARIABLE_RE.sub(" ", STRING_RE.sub(" ", expr))
    if LOGQL_RE.search(text):
        return set()
    text = MODIFIER_RE.sub(" ", re.sub(r"\{[^}]*\}|\[[^\]]*\]", " ", text))
    text = re.sub(r"\b\d[\w.]*", " ", text)
    names = {m.group(0) for m in IDENT_RE.finditer(text)
             if m.group(0) not in PROMQL_WORDS and not m.group(0).startswith("__")
             and not text[m.end():].lstrip().startswith("(")}
    if HISTOGRAM_FN_RE.search(expr):
        for base in {family_base(n) for n in names} - {None}:
            names |= {base + s for s in PAIR_SUFFIXES}
    return names


def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_consumers(paths: list[str]) -> set[str]:
    """Metric names read by the dashboards and alert rules in these JSON files or directories.
    A path that is neither raises, as does a file that is not JSON: a consumer set silently
    missing its input is a green result over nothing."""
    names: set[str] = set()
    for p in paths:
        if os.path.isfile(p):
            files = [p]
        elif os.path.isdir(p):
            files = sorted(os.path.join(d, f) for d, _, fs in os.walk(p) for f in fs
                           if f.endswith(".json"))
        else:
            raise FileNotFoundError(f"--consumers path is neither a file nor a directory: {p}")
        for fp in files:
            for e in expressions(load_json(fp)):
                names |= metric_names(e)
    return names


def evidence(item: dict, consumers: set[str]) -> str:
    """Strongest first: 'file' (a supplied dashboard or rule reads it), 'rule' / 'dashboard' (the
    engine's own counts), 'queries' (ad-hoc only), 'none'."""
    if item.get("metric") in consumers:
        return "file"
    if item.get("usages_in_rules") or 0:
        return "rule"
    if (item.get("usages_in_dashboards") or 0) or item.get("dashboard_uids"):
        return "dashboard"
    return "queries" if (item.get("usages_in_queries") or 0) else "none"


def usage_text(item: dict) -> str:
    return (f"{item.get('usages_in_queries') or 0} queries, "
            f"{item.get('usages_in_dashboards') or 0} dashboards, "
            f"{item.get('usages_in_rules') or 0} rules")


# --------------------------------------------------------------------------- per-row checks
def check_remove(item: dict, consumers: set[str]) -> Verdict | None:
    """A remove un-aggregates the metric back to its raw series; only a consumer that needs the
    labels justifies that, and a query count names none."""
    if item.get("recommended_action") != "remove":
        return None
    now = series_now(item)
    if not now:
        why = "no counts in the export" if now is None else "0 series"
        return verdict("REMOVE_GONE", item, "apply",
                       f"{why}: the metric is gone, the rule is housekeeping")
    ev = evidence(item, consumers)
    if ev in CONSUMED:
        who = "a supplied dashboard or rule" if ev == "file" else f"the engine counts a {ev}"
        return verdict("REMOVE_HAS_CONSUMER", item, "review", f"{usage_text(item)}; {who} reads "
                       "it: check whether the current rule's kept labels already serve it")
    return verdict("REMOVE_QUERY_ONLY", item, "reject", f"{usage_text(item)}: a query is not a "
                   "consumer; who ran it is not in the export and the count ages out")


def check_protected(item: dict, rule: dict | None, protected: list[str]) -> Verdict | None:
    """An add or update that drops a protected label. For an update only a label the live rule
    does not already drop counts, or every cluster-less runtime rule is relisted."""
    if item.get("recommended_action") not in ("add", "update"):
        return None
    already = set((rule or {}).get("drop_labels") or [])
    hit = sorted(set(item.get("drop_labels") or []) & set(protected) - already)
    if not hit:
        return None
    return verdict("DROPS_PROTECTED", item, "modify",
                   f"drops {','.join(hit)}: keep it, drop the rest; the saving shrinks a little")


def check_count_only(item: dict) -> Verdict | None:
    """count stores the number of contributing series. Only an _info gauge, whose every series
    is 1, survives it (there sum == count). In a keep row the defect is the live rule's."""
    action = item.get("recommended_action")
    if (action not in ("add", "update", "keep") or set(item.get("aggregations") or []) != {"count"}
            or item.get("metric", "").endswith("_info")):
        return None
    if action == "keep":
        return verdict("LIVE_RULE_SHAPE", item, "modify", "the live rule is count alone: the "
                       "value is already the number of series; fix the rule")
    return verdict("COUNT_ONLY", item, "reject",
                   "aggregation is count alone: the value would become the number of series")


def check_max(item: dict) -> Verdict | None:
    """A _max gauge summed across pods is not a maximum of anything. In a keep row the defect is
    the live rule's."""
    action, aggs = item.get("recommended_action"), set(item.get("aggregations") or [])
    if (action not in ("add", "update", "keep") or not item.get("metric", "").endswith("_max")
            or not aggs - {"max"}):
        return None
    shape = ",".join(sorted(aggs))
    if action == "keep":
        return verdict("LIVE_RULE_SHAPE", item, "modify", f"the live rule aggregates a _max with "
                       f"{shape}: only max is valid; fix the rule")
    return verdict("MAX_AS_SUM", item, "modify",
                   f"aggregations {shape} on a _max metric: only max is valid")


def check_restore(item: dict, rule: dict | None, consumers: set[str]) -> Verdict | None:
    """An update whose drop set is smaller than the live rule's restores labels, which costs
    series. Only a consumer that reads the label justifies it; a query count is not one."""
    if item.get("recommended_action") != "update" or rule is None:
        return None
    current, proposed = set(rule.get("drop_labels") or []), set(item.get("drop_labels") or [])
    restored, dropped = ",".join(sorted(current - proposed)), ",".join(sorted(proposed - current))
    if not restored:
        return None
    if evidence(item, consumers) in CONSUMED:
        return verdict("RESTORES_LABELS", item, "review",
                       f"restores {restored}; a consumer reads the metric: confirm it groups by them")
    if dropped:
        return verdict("RESTORES_AND_DROPS", item, "modify",
                       f"restores {restored} with no consumer; take only the new drops {dropped}")
    return verdict("RESTORES_LABELS", item, "reject",
                   f"only restores {restored}; {usage_text(item)}: no consumer asked for them")


# --------------------------------------------------------------------------- across rows
def end_state(item: dict | None, rule: dict | None, rejected: bool = False) -> frozenset | None:
    """The drop set a metric ends with once the verdicts are followed; None = raw, no rule. A
    rejected row leaves the live state in place, whatever the engine proposed."""
    action = (item or {}).get("recommended_action")
    if rejected or action in (None, "keep"):
        if rule is not None:
            return frozenset(rule.get("drop_labels") or [])
        return frozenset(item.get("drop_labels") or []) if action == "keep" else None
    if action in ("add", "update"):
        return frozenset(item.get("drop_labels") or [])
    return None     # a remove that is followed


def check_pairs(items: list[dict], rules: dict[str, dict], consumers: set[str],
                verdicts: list[Verdict] = ()) -> list[Verdict]:
    """A histogram or summary is one object stored as two or three series families, which must
    end with the same drop set or every average and quantile join breaks; the engine recommends
    per metric, so it splits pairs and never mends them. The end state follows the per-row
    verdicts: a rejected remove keeps its rule. The universe is every metric the inputs name; a
    side whose sibling appears nowhere is PAIR_LONE -- the export cannot say whether the sibling
    exists."""
    queue = {it["metric"]: it for it in items if "metric" in it}
    rejected = {v.metric for v in verdicts if v.verdict == "reject"}
    families: dict[str, set[str]] = {}
    for metric in set(queue) | set(rules) | set(consumers):
        if family_base(metric):
            families.setdefault(family_base(metric), set()).add(metric)
    out = []
    for base, members in sorted(families.items()):
        ends = {m: end_state(queue.get(m), rules.get(m), m in rejected) for m in sorted(members)}
        if len(members) == 1:
            (metric,) = members
            if ends[metric] is not None:
                others = "/".join(s for s in PAIR_SUFFIXES if not metric.endswith(s))
                out.append(Verdict("PAIR_LONE", metric, None, "review", f"no {others} sibling in "
                                   "any input: if one exists it is unaggregated; mirror this drop "
                                   "set onto it"))
        elif len(set(ends.values())) > 1:
            shape = ", ".join(f"{m[len(base):]} {'raw' if e is None else f'{len(e)} dropped'}"
                              for m, e in ends.items())
            out.append(Verdict("PAIR_SPLIT", base + "_*",
                               sum(delta(queue[m]) or 0 for m in members if m in queue), "modify",
                               f"siblings end with different drop sets ({shape}): mirror deliberately"))
    return out


def review(items: list[dict], rules: dict[str, dict], consumers: set[str],
           protected: list[str]) -> list[Verdict]:
    out: list[Verdict] = []
    for it in items:
        rule = rules.get(it.get("metric", ""))
        out += [v for v in (check_remove(it, consumers), check_protected(it, rule, protected),
                            check_count_only(it), check_max(it), check_restore(it, rule, consumers))
                if v]
    return out + check_pairs(items, rules, consumers, out)


def passes(items: list[dict], verdicts: list[Verdict]) -> list[dict]:
    """Add/update rows no check touched, split-pair members excluded: apply as recommended."""
    flagged = {v.metric for v in verdicts if v.code != "PAIR_LONE"}
    return [it for it in items if it.get("recommended_action") in ("add", "update")
            and it.get("metric") not in flagged
            and f"{family_base(it.get('metric', ''))}_*" not in flagged]


def net_if_followed(items: list[dict], verdicts: list[Verdict]) -> int:
    """Passes, housekeeping, and modify rows at the engine's estimate -- an upper bound on the
    saving. Each metric counts once; a row rejected by one check and modified by another is out,
    and a keep row's estimate is stale statistics, not a change."""
    rejected = {v.metric for v in verdicts if v.verdict == "reject"}
    followed = {v.metric for v in verdicts if v.verdict in ("apply", "modify")} - rejected
    followed |= {it.get("metric") for it in passes(items, verdicts)}
    return sum(delta(it) or 0 for it in items
               if it.get("metric") in followed and it.get("recommended_action") != "keep")


# --------------------------------------------------------------------------- entrypoint
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recommendations", required=True,
                    help="GET /aggregations/recommendations?verbose=true")
    ap.add_argument("--rules", help="GET /aggregations/rules, or the repository's rules file")
    ap.add_argument("--consumers", nargs="+", default=[], metavar="PATH",
                    help="dashboard / alert-rule JSON files or directories; expr fields are read")
    ap.add_argument("--protect-label", action="append", metavar="LABEL",
                    help="never drop this label (repeatable; default: cluster)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    protected = args.protect_label or ["cluster"]

    try:
        items = load_json(args.recommendations)
        items = items.get("items", items) if isinstance(items, dict) else items
        rules = {r["metric"]: r for r in (load_json(args.rules) if args.rules else [])
                 if "metric" in r}
        consumers = load_consumers(args.consumers)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read an input: {exc}", file=sys.stderr)
        return 2
    if not items:
        print("REFUSING a green result: the recommendation export holds no rows. Check the stack, "
              "the token and that ?verbose=true was requested.", file=sys.stderr)
        return 3
    warnings = []
    if not consumers:
        how = "no --consumers given" if not args.consumers else "--consumers yielded 0 metric names"
        warnings.append(f"{how}: removes and restores are judged on the engine's counts alone, "
                        "which say that a query happened and not who reads the metric")
    if not args.rules:
        warnings.append("no --rules given: updates cannot be compared with the live rule, so "
                        "restored labels are invisible and already-dropped protected labels are "
                        "relisted")

    by_action = direction(items)
    verdicts = review(items, rules, consumers, protected)
    lone = [v for v in verdicts if v.code == "PAIR_LONE"]
    rows = [v for v in verdicts if v.code != "PAIR_LONE"]
    passed = passes(items, verdicts)
    pass_delta = sum(delta(it) or 0 for it in passed)
    net = net_if_followed(items, verdicts)
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.verdict] = counts.get(v.verdict, 0) + 1

    if args.format == "json":
        print(json.dumps({
            "queue": {"rows": by_action["total"][0], "net": by_action["total"][1],
                      "by_action": {a: {"rows": n, "delta": d}
                                    for a, (n, d) in by_action.items() if a != "total"}},
            "verdicts": [asdict(v) for v in rows],
            "not_recommended_by_engine": [asdict(v) for v in lone],
            "passes": {"rows": len(passed), "delta": pass_delta}, "net_if_followed": net,
            "consumer_metrics": len(consumers), "protected_labels": protected,
            "warnings": warnings}, indent=1))
    else:
        parts = " | ".join(f"{a} {n} {d:+d}" for a, (n, d) in by_action.items()
                           if a != "total" and n)
        print(f"queue {by_action['total'][0]} rows: {parts} | net {by_action['total'][1]:+d}\n")
        for v in sorted(rows, key=lambda v: (v.verdict != "reject", v.code, -abs(v.delta or 0),
                                             v.metric)):
            print(v.line())
        if lone:
            print("\nnot recommended by the engine (pair consistency; the sibling's series are not "
                  "in the export):")
            print("\n".join(v.line() for v in lone))
        print(f"\n{len(passed)} add/update rows pass every check: apply as recommended "
              f"({pass_delta:+d} series)")
        summary = ", ".join(f"{k} {n}" for k, n in sorted(counts.items())) or "none"
        print(f"verdicts: {summary}; net if followed {net:+d} series (modify rows at the engine's "
              f"estimate); {len(consumers)} consumer metric names read")
    for w in warnings:
        print(f"WARNING {w}", file=sys.stderr)

    if not verdicts and not consumers:
        print("REFUSING a green result: nothing to decide, and no consumer input to decide it "
              "against.", file=sys.stderr)
        return 3
    return 1 if verdicts else 0


if __name__ == "__main__":
    sys.exit(main())
