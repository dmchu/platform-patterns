"""Tests for the aggregation recommendation review. One fixture per check code in review.py,
plus the optional-field tolerance of the export, the refusal of a green result over nothing, and
an end-to-end run over a small synthetic queue. Every name here is invented."""

import json

import pytest

import review

NONE = set()


def row(metric, action, drop=("pod", "instance"), aggs=("sum:counter",), raw=None, cur=None,
        rec=None, queries=0, dashboards=0, rules=0, **extra):
    """A verbose export row. Count fields are optional per row in the real export, so None omits
    them."""
    it = {"metric": metric, "recommended_action": action, "drop_labels": list(drop),
          "aggregations": list(aggs), "usages_in_queries": queries,
          "usages_in_dashboards": dashboards, "usages_in_rules": rules}
    for key, value in (("raw_series_count", raw), ("current_series_count", cur),
                       ("recommended_series_count", rec)):
        if value is not None:
            it[key] = value
    it.update(extra)
    return it


def rule(metric, drop=("pod", "instance"), aggs=("sum:counter",)):
    return {"metric": metric, "drop_labels": list(drop), "aggregations": list(aggs)}


def codes(verdicts):
    return sorted(v.code for v in verdicts)


# --------------------------------------------------------------------------- the export's numbers

def test_delta_is_recommended_minus_current_and_a_remove_recommends_the_raw_count():
    assert review.delta(row("app_jobs_total", "remove", raw=650, cur=3, rec=650)) == 647
    assert review.delta(row("app_jobs_total", "add", raw=40, cur=40, rec=12)) == -28
    assert review.delta({"metric": "x", "total_series_before_aggregation": 10,
                         "total_series_after_aggregation": 4}) == -6


def test_direction_sums_rows_and_series_by_action_and_prints_a_total():
    items = [row("a_sum", "remove", raw=100, cur=2, rec=100),
             row("b_total", "add", raw=30, cur=30, rec=10), row("c_total", "update", cur=20, rec=15),
             row("d_total", "keep", cur=9, rec=9), row("e_total", "remove")]
    d = review.direction(items)
    assert d["remove"] == (2, 98) and d["add"] == (1, -20) and d["update"] == (1, -5)
    assert d["keep"] == (1, 0) and d["total"] == (5, 73)


# --------------------------------------------------------------------------- removes

def test_a_remove_backed_only_by_queries_is_rejected_however_many_queries():
    for n in (1, 59):
        v = review.check_remove(row("worker_poll_latency_seconds_sum", "remove", raw=1200, cur=2,
                                    rec=1200, queries=n), NONE)
        assert v.code == "REMOVE_QUERY_ONLY" and v.verdict == "reject" and v.delta == 1198
        assert f"{n} queries, 0 dashboards, 0 rules" in v.detail
        assert "a query is not a consumer" in v.detail


def test_a_remove_read_by_a_dashboard_or_a_supplied_rule_is_a_review_item():
    engine = review.check_remove(row("orders_total", "remove", raw=80, cur=4, rec=80, queries=1,
                                     dashboards=1), NONE)
    assert engine.code == "REMOVE_HAS_CONSUMER" and engine.verdict == "review"
    assert "engine counts a dashboard" in engine.detail
    supplied = review.check_remove(row("orders_total", "remove", raw=80, cur=4, rec=80, queries=1),
                                   {"orders_total"})
    assert supplied.code == "REMOVE_HAS_CONSUMER" and "supplied dashboard or rule" in supplied.detail


def test_a_remove_of_a_metric_with_no_series_is_housekeeping():
    zero = review.check_remove(row("legacy_client_duration_ms_sum", "remove", raw=0, cur=0, rec=0),
                               NONE)
    assert zero.code == "REMOVE_GONE" and zero.verdict == "apply" and zero.delta == 0
    gone = review.check_remove(row("legacy_client_duration_ms_count", "remove"), NONE)  # no counts
    assert gone.code == "REMOVE_GONE" and gone.delta is None
    assert "no counts in the export" in gone.detail


def test_evidence_ranks_a_supplied_file_above_the_engine_counts():
    it = row("orders_total", "add", queries=3, dashboards=1, rules=1)
    assert review.evidence(it, {"orders_total"}) == "file"
    assert review.evidence(it, NONE) == "rule"
    assert review.evidence(row("x", "add", dashboard_uids=["d1"]), NONE) == "dashboard"
    assert review.evidence(row("x", "add", queries=2), NONE) == "queries"
    assert review.evidence(row("x", "add"), NONE) == "none"


# --------------------------------------------------------------------------- shapes

def test_an_add_that_drops_a_protected_label_is_modified_to_keep_it():
    v = review.check_protected(row("db_pool_wait_seconds_count", "add", drop=("cluster", "pod"),
                                   cur=120, rec=11), None, ["cluster"])
    assert v.code == "DROPS_PROTECTED" and v.verdict == "modify"
    assert "drops cluster: keep it" in v.detail
    assert review.check_protected(row("db_pool_wait_seconds_count", "add", drop=("pod",)), None,
                                  ["cluster"]) is None


def test_an_update_already_dropping_the_protected_label_is_not_relisted_and_the_label_is_configurable():
    live = rule("runtime_threads", drop=("cluster", "pod"))
    assert review.check_protected(row("runtime_threads", "update", drop=("cluster", "pod", "zone")),
                                  live, ["cluster"]) is None
    v = review.check_protected(row("runtime_threads", "update", drop=("cluster", "pod", "region")),
                               live, ["cluster", "region"])
    assert v.code == "DROPS_PROTECTED" and "drops region" in v.detail


def test_count_as_the_only_aggregation_destroys_a_value_metric_but_not_an_info_gauge():
    v = review.check_count_only(row("edge_requests_sum", "add", aggs=("count",), cur=67, rec=3))
    assert v.code == "COUNT_ONLY" and v.verdict == "reject" and "number of series" in v.detail
    assert review.check_count_only(row("deployment_info", "keep", aggs=("count",))) is None
    assert review.check_count_only(row("edge_requests_sum", "add", aggs=("count", "sum"))) is None


def test_a_max_metric_aggregated_with_sum_and_count_is_the_wrong_aggregation():
    v = review.check_max(row("queue_depth_max", "update", aggs=("count", "sum", "sum:counter"),
                             cur=40, rec=12))
    assert v.code == "MAX_AS_SUM" and v.verdict == "modify" and "only max is valid" in v.detail
    assert review.check_max(row("queue_depth_max", "add", aggs=("max",))) is None
    # a remove is judged by its consumers instead
    assert review.check_max(row("queue_depth_max", "remove", aggs=("sum",))) is None


def test_a_keep_row_with_a_wrong_shape_is_a_defect_of_the_live_rule_not_of_a_proposal():
    v = review.check_count_only(row("edge_requests_sum", "keep", aggs=("count",), cur=3, rec=3))
    assert v.code == "LIVE_RULE_SHAPE" and v.verdict == "modify"
    assert "the live rule is count alone" in v.detail and "fix the rule" in v.detail
    v = review.check_max(row("queue_depth_max", "keep", aggs=("count", "sum")))
    assert v.code == "LIVE_RULE_SHAPE" and "aggregates a _max with count,sum" in v.detail


def test_an_update_that_only_restores_labels_nobody_reads_is_rejected():
    live = rule("runtime_threads", drop=("pod", "instance", "workload"))
    v = review.check_restore(row("runtime_threads", "update", drop=("pod", "instance"), cur=6, rec=9),
                             live, NONE)
    assert v.code == "RESTORES_LABELS" and v.verdict == "reject" and v.delta == 3
    assert "only restores workload" in v.detail
    assert review.check_restore(row("runtime_threads", "update",
                                    drop=("pod", "instance", "workload", "zone")), live, NONE) is None
    # no live rule to compare with
    assert review.check_restore(row("runtime_threads", "update", drop=("pod",)), None, NONE) is None


def test_an_update_that_restores_and_drops_takes_only_the_drops_unless_a_consumer_reads_it():
    live = rule("runtime_threads", drop=("pod", "workload"))
    mixed = review.check_restore(row("runtime_threads", "update", drop=("pod", "zone")), live, NONE)
    assert mixed.code == "RESTORES_AND_DROPS" and mixed.verdict == "modify"
    assert "take only the new drops zone" in mixed.detail
    read = review.check_restore(row("runtime_threads", "update", drop=("pod", "zone")), live,
                                {"runtime_threads"})
    assert read.code == "RESTORES_LABELS" and read.verdict == "review"


# --------------------------------------------------------------------------- pairs

def test_siblings_that_would_end_with_different_drop_sets_are_a_split():
    items = [row("http_request_duration_seconds_sum", "add", drop=("pod", "route"), cur=90, rec=10)]
    rules = {"http_request_duration_seconds_count":
             rule("http_request_duration_seconds_count", drop=("pod",))}
    (v,) = review.check_pairs(items, rules, NONE)
    assert v.code == "PAIR_SPLIT" and v.metric == "http_request_duration_seconds_*" and v.delta == -80
    assert "_count 1 dropped" in v.detail and "_sum 2 dropped" in v.detail


def test_removing_one_side_of_a_ruled_pair_is_a_split_and_its_members_do_not_pass():
    items = [row("db_pool_wait_seconds_sum", "remove", raw=50, cur=5, rec=50, queries=1),
             row("db_pool_wait_seconds_count", "remove", raw=50, cur=5, rec=50, queries=1),
             row("db_pool_wait_seconds_bucket", "keep", drop=("pod", "instance", "le_ignored"))]
    rules = {m: rule(m) for m in ("db_pool_wait_seconds_sum", "db_pool_wait_seconds_count",
                                  "db_pool_wait_seconds_bucket")}
    verdicts = review.check_pairs(items, rules, NONE)
    assert codes(verdicts) == ["PAIR_SPLIT"]
    assert "_sum raw" in verdicts[0].detail and "_bucket 2 dropped" in verdicts[0].detail
    assert review.passes(items + [row("db_pool_wait_seconds_sum", "add")], verdicts) == []


def test_a_consistent_pair_is_silent():
    rules = {m: rule(m) for m in ("http_request_duration_seconds_sum",
                                  "http_request_duration_seconds_count")}
    assert review.check_pairs([], rules, NONE) == []


def test_a_lone_ruled_side_is_listed_as_a_saving_the_engine_does_not_recommend():
    rules = {"worker_poll_latency_seconds_sum": rule("worker_poll_latency_seconds_sum")}
    (v,) = review.check_pairs([row("worker_poll_latency_seconds_sum", "keep")], rules, NONE)
    assert v.code == "PAIR_LONE" and v.verdict == "review" and v.delta is None
    assert "no _count/_bucket sibling in any input" in v.detail and "mirror this drop set" in v.detail


def test_a_rejected_remove_keeps_its_rule_so_a_lone_ruled_sum_is_still_listed():
    items = [row("worker_poll_latency_seconds_sum", "remove", raw=976, cur=1, rec=976, queries=1)]
    rules = {"worker_poll_latency_seconds_sum": rule("worker_poll_latency_seconds_sum")}
    verdicts = review.review(items, rules, {"app_jobs_total"}, ["cluster"])
    assert codes(verdicts) == ["PAIR_LONE", "REMOVE_QUERY_ONLY"]
    assert verdicts[1].metric == "worker_poll_latency_seconds_sum"
    # read with the engine's eyes (no verdicts) the remove would leave it raw, and nothing is listed
    assert review.check_pairs(items, rules, NONE) == []


def test_a_lone_side_known_only_from_a_consumer_or_left_raw_is_not_listed():
    assert review.check_pairs([], {}, {"orders_value_sum"}) == []
    unruled = [row("orders_value_sum", "remove", raw=9, cur=1, rec=9, queries=1)]
    assert review.check_pairs(unruled, {}, NONE, review.review(unruled, {}, NONE, [])) == []
    gone = [row("orders_value_sum", "remove")]
    assert review.check_pairs(gone, {"orders_value_sum": rule("orders_value_sum")}, NONE,
                              review.review(gone, {}, NONE, [])) == []


# --------------------------------------------------------------------------- consumers

def test_metric_names_skip_functions_keywords_matchers_durations_and_numbers():
    expr = ('sum by (route) (rate(app_jobs_total{status!="ok", job=~"api.*"}[5m] offset 1h)) '
            '/ on (route) group_left runtime_threads > 0.5')
    assert review.metric_names(expr) == {"app_jobs_total", "runtime_threads"}
    assert review.metric_names("absent(orders_total) or vector(0)") == {"orders_total"}
    assert review.metric_names("1e3 * (deployment_info == bool 1)") == {"deployment_info"}


def test_a_histogram_function_expands_the_family():
    names = review.metric_names(
        'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))')
    assert names == {"http_request_duration_seconds_bucket", "http_request_duration_seconds_sum",
                     "http_request_duration_seconds_count"}
    assert review.metric_names("rate(http_request_duration_seconds_sum[5m])") == {
        "http_request_duration_seconds_sum"}


def test_logql_grafana_math_and_template_variables_name_no_metric():
    logql = 'sum(count_over_time({app="api"} |= "error" | json | status >= 500 [5m])) > 0'
    assert review.metric_names(logql) == set()
    assert review.metric_names('{app="api"} |~ `panic.*`') == set()
    assert review.metric_names("$A > 0 && ${B} < 1") == set()
    promql = 'rate(app_jobs_total{cluster="$cluster", job=~"a|b"}[$__rate_interval])'
    assert review.metric_names(promql) == {"app_jobs_total"}


def test_consumers_are_read_from_dashboards_rule_exports_and_flat_lists(tmp_path):
    (tmp_path / "dash").mkdir()
    (tmp_path / "dash" / "d1.json").write_text(json.dumps(
        {"uid": "d1", "panels": [{"targets": [{"expr": "sum(rate(app_jobs_total[5m]))"}]},
                                 {"panels": [{"targets": [{"expr": "orders_total",
                                                           "legendFormat": "{{route}}"}]}]}],
         "tags": ["not_a_metric"]}))
    (tmp_path / "alerts.json").write_text(json.dumps(
        [{"e": ["runtime_threads > 100"], "p": False, "t": "threads", "u": "r1"}]))
    (tmp_path / "flat.json").write_text(json.dumps(["deployment_info == 1"]))
    names = review.load_consumers([str(tmp_path / "dash"), str(tmp_path / "alerts.json"),
                                   str(tmp_path / "flat.json")])
    assert names == {"app_jobs_total", "orders_total", "runtime_threads", "deployment_info"}


def test_a_consumer_file_that_is_not_json_raises_instead_of_silently_shrinking_the_set(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        review.load_consumers([str(tmp_path)])


def test_a_consumer_path_that_does_not_exist_raises_and_an_empty_directory_yields_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        review.load_consumers([str(tmp_path / "dashboards-typo")])
    (tmp_path / "empty").mkdir()
    assert review.load_consumers([str(tmp_path / "empty")]) == set()


# --------------------------------------------------------------------------- optional fields

def test_a_row_with_only_the_mandatory_fields_is_tolerated_everywhere():
    bare = {"metric": "app_jobs_total", "recommended_action": "update", "drop_labels": [],
            "aggregations": []}
    assert review.delta(bare) is None and review.series_now(bare) is None
    assert review.direction([bare]) == {"remove": (0, 0), "update": (1, 0), "add": (0, 0),
                                        "keep": (0, 0), "total": (1, 0)}
    assert review.review([bare, {"recommended_action": "keep"}], {}, NONE, ["cluster"]) == []


def test_a_long_metric_name_is_padded_not_truncated():
    name = "a_" * 30 + "very_long_histogram_family_name_seconds_count"
    assert name in review.Verdict("PAIR_LONE", name, None, "review", "x").line()


# --------------------------------------------------------------------------- the net

def test_net_if_followed_counts_a_metric_once_and_drops_a_row_that_is_also_rejected():
    items = [row("queue_depth_max", "add", aggs=("sum", "count"), drop=("cluster", "pod"), cur=100,
                 rec=10),                                   # MAX_AS_SUM + DROPS_PROTECTED: -90 once
             row("edge_requests_total", "add", aggs=("count",), drop=("cluster", "pod"), cur=50,
                 rec=5)]                                    # COUNT_ONLY reject + DROPS_PROTECTED: out
    verdicts = review.review(items, {}, NONE, ["cluster"])
    assert codes(verdicts) == ["COUNT_ONLY", "DROPS_PROTECTED", "DROPS_PROTECTED", "MAX_AS_SUM"]
    assert review.net_if_followed(items, verdicts) == -90


# --------------------------------------------------------------------------- the entrypoint

def write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def test_an_empty_queue_is_refused(tmp_path, capsys):
    assert review.main(["--recommendations", write(tmp_path, "q.json", [])]) == 3
    assert "REFUSING" in capsys.readouterr().err


def test_a_green_result_without_consumer_input_is_refused_and_warned(tmp_path, capsys):
    q = write(tmp_path, "q.json", [row("app_jobs_total", "add", drop=("pod",), cur=30, rec=10)])
    assert review.main(["--recommendations", q]) == 3
    err = capsys.readouterr().err
    assert "WARNING no --consumers" in err and "REFUSING a green result" in err
    c = write(tmp_path, "c.json", ["orders_total"])
    assert review.main(["--recommendations", q, "--consumers", c]) == 0
    assert "REFUSING" not in capsys.readouterr().err


def test_consumers_that_yield_no_names_count_as_no_consumers(tmp_path, capsys):
    q = write(tmp_path, "q.json", [row("app_jobs_total", "add", drop=("pod",), cur=30, rec=10)])
    (tmp_path / "empty").mkdir()
    assert review.main(["--recommendations", q, "--consumers", str(tmp_path / "empty")]) == 3
    err = capsys.readouterr().err
    assert "WARNING --consumers yielded 0 metric names" in err and "REFUSING" in err


def test_an_unreadable_input_exits_2_with_the_path_named_not_a_traceback(tmp_path, capsys):
    q = write(tmp_path, "q.json", [row("app_jobs_total", "add", drop=("pod",), cur=30, rec=10)])
    assert review.main(["--recommendations", str(tmp_path / "nope.json")]) == 2
    assert "nope.json" in capsys.readouterr().err
    assert review.main(["--recommendations", q, "--consumers", str(tmp_path / "typo")]) == 2
    assert "neither a file nor a directory" in capsys.readouterr().err
    assert review.main(["--recommendations", q, "--rules", str(tmp_path / "r.json")]) == 2


def test_exit_1_whenever_a_verdict_is_printed_including_review_apply_and_lone(tmp_path, capsys):
    c = write(tmp_path, "c.json", ["app_jobs_total"])
    for name, summary, queue, rules in (
            ("review", "review 1",
             [row("orders_total", "remove", raw=80, cur=4, rec=80, dashboards=1)], []),
            ("apply", "apply 1", [row("legacy_client_duration_ms_sum", "remove")], []),
            ("lone", "review 1", [row("http_request_duration_seconds_sum", "keep")],
             [rule("http_request_duration_seconds_sum")])):
        assert review.main(["--recommendations", write(tmp_path, f"{name}.json", queue),
                            "--rules", write(tmp_path, f"{name}-r.json", rules),
                            "--consumers", c]) == 1
        out = capsys.readouterr().out
        assert f"verdicts: {summary};" in out and (name != "lone" or "PAIR_LONE" in out)


def test_json_output_carries_the_direction_and_the_warnings(tmp_path, capsys):
    q = write(tmp_path, "q.json", [row("a_total", "remove", raw=100, cur=2, rec=100, queries=1)])
    assert review.main(["--recommendations", q, "--format", "json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["queue"] == {"rows": 1, "net": 98, "by_action": {
        "remove": {"rows": 1, "delta": 98}, "update": {"rows": 0, "delta": 0},
        "add": {"rows": 0, "delta": 0}, "keep": {"rows": 0, "delta": 0}}}
    assert out["verdicts"][0]["code"] == "REMOVE_QUERY_ONLY" and len(out["warnings"]) == 2


# expected: reject +1198, review +76, apply (no counts), modify -109, reject -64, passes -20,
# modify -28, reject +3, silent (_info)
QUEUE = [
    row("worker_poll_latency_seconds_sum", "remove", raw=1200, cur=2, rec=1200, queries=1),
    row("orders_total", "remove", raw=80, cur=4, rec=80, queries=1, dashboards=1),
    row("legacy_client_duration_ms_sum", "remove"),
    row("db_pool_wait_seconds_count", "add", drop=("cluster", "pod"), cur=120, rec=11),
    row("edge_requests_sum", "add", aggs=("count",), cur=67, rec=3),
    row("app_jobs_total", "add", drop=("pod",), cur=30, rec=10),
    row("queue_depth_max", "update", aggs=("count", "sum", "sum:counter"), drop=("pod", "zone"),
        cur=40, rec=12),
    row("runtime_threads", "update", drop=("pod", "instance"), cur=6, rec=9),
    row("deployment_info", "keep", aggs=("count",), cur=13, rec=13),
]
RULES = [rule("worker_poll_latency_seconds_sum"),
         rule("queue_depth_max", drop=("pod",), aggs=("max",)),
         rule("runtime_threads", drop=("pod", "instance", "workload")),
         rule("deployment_info", aggs=("count",)), rule("legacy_client_duration_ms_sum")]


def test_end_to_end_over_a_small_queue(tmp_path, capsys):
    consumers = write(tmp_path, "alerts.json", [{"e": ["sum(rate(orders_total[5m])) > 10"],
                                                 "p": False, "t": "orders", "u": "r1"}])
    code = review.main(["--recommendations", write(tmp_path, "q.json", QUEUE),
                        "--rules", write(tmp_path, "r.json", RULES), "--consumers", consumers])
    out, err = capsys.readouterr()
    assert code == 1 and err == ""
    assert out.splitlines()[0] == ("queue 9 rows: remove 3 +1274 | update 2 -25 | add 3 -193 | "
                                   "keep 1 +0 | net +1056")
    first = [line.split()[0] for line in out.splitlines() if line and line[0].isupper()]
    assert first == ["COUNT_ONLY", "REMOVE_QUERY_ONLY", "RESTORES_LABELS", "DROPS_PROTECTED",
                     "MAX_AS_SUM", "REMOVE_GONE", "REMOVE_HAS_CONSUMER", "PAIR_LONE", "PAIR_LONE"]
    lone = [line.split()[1] for line in out.splitlines() if line.startswith("PAIR_LONE")]
    # the rejected remove keeps its rule and stays lone; the rejected count-only add has no rule
    assert lone == ["db_pool_wait_seconds_count", "worker_poll_latency_seconds_sum"]
    assert "not recommended by the engine" in out
    assert "1 add/update rows pass every check: apply as recommended (-20 series)" in out
    assert "verdicts: apply 1, modify 2, reject 3, review 3; net if followed -157 series" in out
    assert "1 consumer metric names read" in out
