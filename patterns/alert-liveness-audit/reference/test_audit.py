"""Tests for the liveness audit. Each fixture is one of the dead-rule shapes from the README,
or one of the four false positives the first run on a real estate produced."""

import pytest

import audit

LOGS = {"loki-1"}


def rule(uid="r1", title="rule", condition="C", for_="0s", nodata="OK", exprs=(), cond_type="threshold",
         ev="gt", params=(0,), ds="prom-1", updated="2026-01-01T00:00:00Z"):
    data = [{"refId": chr(65 + i), "datasourceUid": ds, "model": {"expr": e}} for i, e in enumerate(exprs)]
    data.append({"refId": condition, "datasourceUid": "__expr__",
                 "model": {"type": cond_type,
                           "conditions": [{"evaluator": {"type": ev, "params": list(params)}}]}})
    return {"uid": uid, "title": title, "condition": condition, "for": for_, "noDataState": nodata,
            "data": data, "updated": updated}


def kinds(items):
    return sorted(f.kind for f in items)


# --------------------------------------------------------------------------- definition checks

def test_threshold_node_over_a_continuous_signal_is_fine():
    assert audit.check_definition(rule(exprs=["sum(rate(x[5m]))"], for_="2m"), LOGS) == []


def test_a_real_threshold_on_a_reduce_node_is_dead():
    f = audit.check_definition(rule(cond_type="reduce", params=(2,), exprs=["sum(x)"]), LOGS)
    assert kinds(f) == ["DEAD_EVALUATOR"]
    assert "gt [2] is ignored" in f[0].detail


def test_a_zero_evaluator_on_a_reduce_node_is_only_a_review_item():
    f = audit.check_definition(rule(cond_type="reduce", params=(0, 0), exprs=["sum(x)"]), LOGS)
    assert kinds(f) == ["EVALUATOR_ON_REDUCE"]
    assert not f[0].is_finding


def test_for_at_least_the_window_on_a_single_event_log_rule_cannot_fire():
    f = audit.check_definition(
        rule(for_="15m", ds="loki-1", exprs=['sum(count_over_time({app="x"} |= "boom" [5m]))']), LOGS)
    assert kinds(f) == ["FOR_ON_SINGLE_EVENT"]
    assert f[0].is_finding


def test_for_at_least_the_window_on_a_continuous_metric_is_only_a_review_item():
    f = audit.check_definition(rule(for_="15m", exprs=["sum(rate(http_errors[5m]))"]), LOGS)
    assert kinds(f) == ["FOR_EXCEEDS_WINDOW"]
    assert not f[0].is_finding


def test_for_shorter_than_the_window_is_not_flagged():
    assert audit.check_definition(rule(for_="2m", exprs=["avg_over_time(x[5m])"]), LOGS) == []


def test_compound_durations_parse():
    assert audit.seconds("1h30m") == 5400
    assert audit.seconds("0s") == 0
    assert audit.seconds(None) == 0


def test_too_many_rule_with_vector0_and_nodata_ok_needs_a_liveness_pair():
    f = audit.check_definition(rule(exprs=["sum(count_over_time(x[1h])) or vector(0)"], params=(5,)), LOGS)
    assert kinds(f) == ["TOO_MANY_NO_LIVENESS"]
    assert not f[0].is_finding


def test_absence_rule_with_vector0_is_the_correct_idiom():
    f = audit.check_definition(rule(exprs=["sum(count_over_time(x[1h])) or vector(0)"], ev="lt", params=(1,)), LOGS)
    assert f == []


# --------------------------------------------------------------------------- history checks

def hist(*states, step=60, start=1_000_000, key="a"):
    return [(start + i * step, s, key) for i, s in enumerate(states)]


def test_pending_without_alerting_reports_the_episode_lengths():
    h = hist("Normal (NoData)", "Pending", "Normal (NoData)", "Pending", "Normal (NoData)")
    f = audit.classify_history("u", "t", h, pending_s=300)
    assert f.kind == "PENDING_NEVER_ALERTING" and f.is_finding
    assert "2 pending episodes, 0 alerting" in f.detail
    assert "longest 1m, median 1m of for=5m (20%)" in f.detail


def test_an_open_pending_episode_is_measured_to_the_window_end():
    h = hist("Normal", "Pending")
    f = audit.classify_history("u", "t", h, pending_s=21600, until_s=h[-1][0] + 7200)
    assert "longest 2h" in f.detail


def test_episodes_are_measured_per_instance_not_across_them():
    """Two instances interleave; instance a's episode is 300s, not the 10s to b's entry."""
    h = sorted([(1000, "Pending", "a"), (1010, "Normal", "b"), (1300, "Normal", "a"), (1400, "Pending", "b")])
    assert audit.pending_episodes(h, until_s=1500) == [300, 100]
    f = audit.classify_history("u", "t", h, pending_s=600)
    assert "2 pending episodes over 2 instances" in f.detail and "longest 5m" in f.detail


def test_a_rule_that_alerted_is_alive():
    assert audit.classify_history("u", "t", hist("Normal", "Pending", "Alerting", "Normal"), 300) is None


def test_only_nodata_means_no_signal():
    f = audit.classify_history("u", "t", hist("Normal (NoData)", "NoData", "Normal (NoData)"))
    assert f.kind == "NODATA_ONLY" and f.is_finding


def test_only_nodata_on_a_young_rule_is_a_review_item():
    f = audit.classify_history("u", "t", hist("Normal (NoData)"), young=True)
    assert f.kind == "YOUNG" and not f.is_finding


def test_no_history_is_quiet_not_dead():
    f = audit.classify_history("u", "t", [])
    assert f.kind == "QUIET" and not f.is_finding


# --------------------------------------------------------------------------- the positive control

class Empty:
    def rules(self):
        return []

    def log_datasource_uids(self):
        return set()

    def history(self, uid, since, until):
        return []


class OneEstate:
    def rules(self):
        return [
            rule(uid="dead", for_="5m", ds="loki-1", exprs=['count_over_time({a="b"} |= "x" [5m])']),
            rule(uid="quiet", for_="1m", exprs=["sum(rate(x[5m]))"], params=(10,)),
            {"uid": "rec", "record": {"metric": "x:rate"}, "data": []},   # never audited
        ]

    def log_datasource_uids(self):
        return LOGS

    def history(self, uid, since, until):
        return hist("Pending", "Normal (NoData)", "Pending", "Normal (NoData)") if uid == "dead" else []


def test_a_report_over_nothing_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("GRAFANA_URL", "https://example.invalid")
    monkeypatch.setenv("GRAFANA_TOKEN", "t")
    monkeypatch.setattr(audit, "Grafana", lambda url, token: Empty())
    assert audit.main(["--days", "7"]) == 3
    assert "REFUSING" in capsys.readouterr().err


def test_findings_and_review_are_separated_and_counted(monkeypatch, capsys):
    monkeypatch.setenv("GRAFANA_URL", "https://example.invalid")
    monkeypatch.setenv("GRAFANA_TOKEN", "t")
    monkeypatch.setattr(audit, "Grafana", lambda url, token: OneEstate())
    assert audit.main(["--days", "7"]) == 1
    out = capsys.readouterr().out
    assert "FOR_ON_SINGLE_EVENT" in out and "PENDING_NEVER_ALERTING" in out
    assert "QUIET                    1 rules had no transitions" in out
    assert "audited 2 rules, 4 history entries: 2 findings, 1 to review" in out
