"""Tests for the liveness audit. Each fixture is one of the dead-rule shapes from the README."""

import pytest

import audit


def rule(uid="r1", title="rule", condition="C", for_="0s", nodata="OK", exprs=(), cond_type="threshold",
         evaluator=(0,)):
    data = [{"refId": chr(65 + i), "model": {"expr": e}} for i, e in enumerate(exprs)]
    data.append({"refId": condition, "model": {"type": cond_type,
                                                "conditions": [{"evaluator": {"type": "gt", "params": list(evaluator)}}]}})
    return {"uid": uid, "title": title, "condition": condition, "for": for_, "noDataState": nodata, "data": data}


def kinds(findings):
    return sorted(f.kind for f in findings)


# --------------------------------------------------------------------------- definition checks

def test_threshold_node_is_fine():
    assert audit.check_definition(rule(exprs=["sum(rate(x[5m]))"])) == []


def test_evaluator_on_a_reduce_node_is_dead_config():
    f = audit.check_definition(rule(cond_type="reduce", evaluator=(2,), exprs=["sum(x)"]))
    assert kinds(f) == ["EVALUATOR_ON_REDUCE"]
    assert "evaluator [2] is ignored" in f[0].detail


def test_pending_period_at_least_the_window_is_flagged():
    f = audit.check_definition(rule(for_="5m", exprs=["avg_over_time(x[5m])"]))
    assert kinds(f) == ["FOR_EXCEEDS_WINDOW"]
    assert audit.check_definition(rule(for_="2m", exprs=["avg_over_time(x[5m])"])) == []


def test_compound_durations_parse():
    assert audit.seconds("1h30m") == 5400
    assert audit.seconds("0s") == 0
    assert audit.seconds(None) == 0


def test_nodata_ok_with_vector0_hides_an_outage():
    f = audit.check_definition(rule(nodata="OK", exprs=["sum(count_over_time(x[1h])) or vector(0)"]))
    assert kinds(f) == ["NODATA_OK_WITH_VECTOR0"]
    assert audit.check_definition(rule(nodata="Alerting", exprs=["sum(x) or vector(0)"])) == []


# --------------------------------------------------------------------------- history checks

def test_pending_without_alerting_is_dead():
    states = ["Normal (NoData)", "Pending", "Normal (NoData)", "Pending", "Normal (NoData)"]
    f = audit.classify_history("u", "t", states)
    assert f.kind == "PENDING_NEVER_ALERTING" and "2 pending" in f.detail


def test_a_rule_that_alerted_is_alive():
    assert audit.classify_history("u", "t", ["Normal", "Pending", "Alerting", "Normal"]) is None


def test_only_nodata_means_no_signal():
    assert audit.classify_history("u", "t", ["Normal (NoData)", "NoData", "Normal (NoData)"]).kind == "NODATA_ONLY"


def test_no_history_is_reported_not_ignored():
    assert audit.classify_history("u", "t", []).kind == "NO_HISTORY"


# --------------------------------------------------------------------------- the positive control

class Empty:
    def rules(self):
        return []

    def history_states(self, uid, since, until):
        return []


class OneDeadRule:
    def rules(self):
        return [rule(uid="dead", for_="5m", exprs=["avg_over_time(x[5m])"])]

    def history_states(self, uid, since, until):
        return ["Pending", "Normal (NoData)"] * 3


def test_a_report_over_nothing_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("GRAFANA_URL", "https://example.invalid")
    monkeypatch.setenv("GRAFANA_TOKEN", "t")
    monkeypatch.setattr(audit, "Grafana", lambda url, token: Empty())
    assert audit.main(["--days", "7"]) == 3
    assert "REFUSING" in capsys.readouterr().err


def test_findings_exit_nonzero_and_are_listed(monkeypatch, capsys):
    monkeypatch.setenv("GRAFANA_URL", "https://example.invalid")
    monkeypatch.setenv("GRAFANA_TOKEN", "t")
    monkeypatch.setattr(audit, "Grafana", lambda url, token: OneDeadRule())
    assert audit.main(["--days", "7"]) == 1
    out = capsys.readouterr().out
    assert "PENDING_NEVER_ALERTING" in out and "FOR_EXCEEDS_WINDOW" in out
    assert "audited 1 rules, 6 history entries: 2 findings" in out
