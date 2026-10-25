# -*- coding: utf-8 -*-
"""일일 점검 경고 레지스트리(daily.py)·자체검증 비교(checks.compare_checks) 회귀 테스트.

store 경로를 전부 tmp_path 로 돌려 실제 data/log 를 건드리지 않는다. run_daily 는 끝까지 돌리지 않는다(네트워크·API).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import checks  # noqa: E402
import daily  # noqa: E402
import store  # noqa: E402


@pytest.fixture
def store_tmp(tmp_path, monkeypatch):
    log = tmp_path / "log"
    for n, p in {"MP_DIR": tmp_path / "mp", "CHECK_DIR": tmp_path / "checks", "LOG_DIR": log,
                 "DAILY_DIR": log / "daily", "LOG_FILE": log / "events.jsonl",
                 "HEAD_FILE": log / "head.json", "ALERTS_FILE": log / "alerts.json"}.items():
        monkeypatch.setattr(store, n, p)
    monkeypatch.setattr(store.config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.delenv("LOG_HMAC_KEY", raising=False)
    return tmp_path


def _alert(code, grade, bucket):
    return {"code": code, "grade": grade, "grade_ko": daily.limits.STAGE_KO[grade], "bucket": bucket,
            "metric": {"name": "m", "value": 1, "threshold": None, "as_of": None, "source": "CODE"},
            "message": f"{bucket} {grade}", "required_action": f"조치 {grade}", "due": "당일"}


# ── 데이터 저하 날: 자동 종결 보류 ──
def test_sync_alerts_defers_close_when_degraded(store_tmp):
    """데이터가 저하된 날에는 사라진 경고를 자동 종결하지 않고, 보류를 기록한다."""
    aid = daily.sync_alerts([_alert("STOP_APPROACH", "WARN", "E1")])["new"][0]["alert_id"]
    r = daily.sync_alerts([], degraded=["Refinitiv 미연결"])
    assert r["closed"] == [] and len(r["open"]) == 1 and r["deferred"][0]["alert_id"] == aid
    assert any(x["kind"] == "CLOSE_DEFERRED" and x["alert_ids"] == [aid] for x in store.read_log(limit=None))
    r2 = daily.sync_alerts([])
    assert len(r2["closed"]) == 1 and r2["open"] == [] and r2["deferred"] == []


def test_degraded_ignores_stale_but_priced_positions():
    """손익 기준일만 지난(가격은 받은) 포지션은 저하가 아니다 — 연휴·샘플에서 자동 종결이 막히면 안 된다(설계 (i))."""
    stale = {"positions": [{"name": "미국 주식", "stale": True, "unpriced_legs": []}]}
    assert daily._degraded(None, stale) == []
    assert daily._degraded({"book": "b", "positions_path": "p"}, stale) == []
    assert daily._degraded({"error": "PermissionError: 잠김"}, stale) == ["PermissionError: 잠김"]
    assert daily._degraded({"skipped": True}, stale) == ["보유 평가 건너뜀"]
    unp = {"positions": [{"name": "E1", "stale": True, "unpriced_legs": ["SPY"]}]}
    assert daily._degraded(None, unp) == ["가격 미수신 포지션: E1"]


def test_degraded_defers_deescalation_but_not_escalation(store_tmp):
    """저하된 날에는 축소→경고 하향을 보류(등급·조치 유지, 로그) — 승격은 그대로 즉시 적용."""
    aid = daily.sync_alerts([_alert("STOP_HIT", "REDUCE", "E1")])["new"][0]["alert_id"]
    r = daily.sync_alerts([_alert("STOP_APPROACH", "WARN", "E1")], degraded=["가격 미수신 포지션: E1"])
    a = store.load_alerts()[0]
    assert a["grade"] == "REDUCE" and a["code"] == "STOP_HIT" and a["required_action"] == "조치 REDUCE"
    assert "DEESCALATED" not in [h["status"] for h in a["history"]] and a["message"] == "E1 WARN"
    assert [x["alert_id"] for x in r["deesc_deferred"]] == [aid] and r["deferred"] == []
    assert any(x["kind"] == "DEESCALATE_DEFERRED" and x["alert_id"] == aid for x in store.read_log(limit=None))
    daily.sync_alerts([_alert("STOP_APPROACH", "WARN", "E1")])
    a = store.load_alerts()[0]
    assert a["grade"] == "WARN" and a["code"] == "STOP_APPROACH" and a["history"][-1]["status"] == "DEESCALATED"
    daily.sync_alerts([_alert("STOP_HIT", "REDUCE", "E1")], degraded=["x"])
    a = store.load_alerts()[0]
    assert a["grade"] == "REDUCE" and a["history"][-1]["status"] == "ESCALATED"


# ── 같은 건에서 승격 ──
def test_alert_escalates_in_place_keeps_operator_history(store_tmp):
    """경고→축소로 나빠지면 같은 경고에서 승격(다시 OPEN) — '조건 해소' 종결·새 경고가 생기지 않고 운용역 이력 유지.
    예전 조치·확인은 이력에만 남기고 현재 필드는 비운다(화면에 '조치: …'가 현재 조치처럼 보이지 않게)."""
    aid = daily.sync_alerts([_alert("UTIL_STAGE", "WARN", "전체")])["new"][0]["alert_id"]
    daily.act_alert(aid, "ACTIONED", "운용역", "대응안")
    r = daily.sync_alerts([_alert("UTIL_STAGE", "REDUCE", "전체")])
    reg = store.load_alerts()
    assert len(reg) == 1 and r["closed"] == [] and r["new"] == []
    a = reg[0]
    assert a["alert_id"] == aid and a["grade"] == "REDUCE" and a["status"] == "OPEN"
    assert a["required_action"] == "조치 REDUCE"
    assert [h["status"] for h in a["history"]] == ["ACTIONED", "ESCALATED"]
    assert a["action"] is None and a["confirm"] is None
    assert a["history"][0]["note"] == "대응안"


def test_stop_alert_family_escalates_and_deescalates(store_tmp):
    """손절 접근(STOP_APPROACH)→도달(STOP_HIT)은 코드가 달라도 같은 계열로 한 건 — 하향은 상태 유지."""
    aid = daily.sync_alerts([_alert("STOP_APPROACH", "WARN", "국고채A")])["new"][0]["alert_id"]
    daily.sync_alerts([_alert("STOP_HIT", "REDUCE", "국고채A")])
    daily.act_alert(aid, "ACTIONED", "운용역", "축소 계획")
    r = daily.sync_alerts([_alert("STOP_APPROACH", "WARN", "국고채A")])
    reg = store.load_alerts()
    assert len(reg) == 1 and r["closed"] == []
    a = reg[0]
    assert a["code"] == "STOP_APPROACH" and a["grade"] == "WARN" and a["status"] == "ACTIONED"
    assert [h["status"] for h in a["history"]] == ["ESCALATED", "ACTIONED", "DEESCALATED"]


def test_dedupe_keeps_higher_grade():
    """한 번 실행에서 같은 계열·버킷 신호가 둘이면 높은 등급 하나로(메시지는 합침)."""
    out = daily._dedupe([_alert("STOP_APPROACH", "WARN", "B1"), _alert("STOP_HIT", "REDUCE", "B1")])
    assert len(out) == 1 and out[0]["grade"] == "REDUCE" and out[0]["code"] == "STOP_HIT"
    assert out[0]["message"] == "B1 WARN / B1 REDUCE"


def test_legacy_open_alerts_merge_to_one(store_tmp):
    """예전 (코드·버킷·등급) 키로 열린 두 건은 높은 등급 하나로 통합하고 다른 건은 사유를 남겨 종결."""
    def rec(i, code, grade):
        return {**_alert(code, grade, "E1"), "alert_id": f"ALT-20260101-0{i}", "status": "OPEN",
                "opened_at": "2026-01-01T09:00:00+09:00", "history": []}
    store.save_alerts([rec(1, "STOP_APPROACH", "WARN"), rec(2, "STOP_HIT", "REDUCE")])
    r = daily.sync_alerts([_alert("STOP_HIT", "REDUCE", "E1")])
    assert [a["alert_id"] for a in r["open"]] == ["ALT-20260101-02"]
    assert r["closed"][0]["alert_id"] == "ALT-20260101-01" and r["closed"][0]["close_reason"] == "등급 단일화(통합)"


def test_monthly_counts_escalations(store_tmp):
    """월간 보고 등급 집계에 승격도 그 등급 발생으로 들어간다(새 경고 대신 ESCALATE 로 남아도 축소 1건 유지)."""
    daily.sync_alerts([_alert("UTIL_STAGE", "WARN", "전체")])
    r = daily.sync_alerts([_alert("UTIL_STAGE", "REDUCE", "전체")])
    kinds = [x["kind"] for x in store.read_log(limit=None)]
    assert r["new"] == [] and len(store.load_alerts()) == 1
    assert kinds.count("ALERT") == 1 and kinds.count("ESCALATE") == 1
    month = store.now_iso()[:7]
    text = daily.monthly_report(month)
    assert "경고 1 / 축소 1 / 중단 0건" in text


def test_daily_markdown_shows_degraded():
    """저하된 날 보고서 머리에 '경고 자동 종결 보류'를 적고, 예전 문서(키 없음)는 그대로."""
    u = {"util_pct": 0, "stage": "NORMAL", "used_bn": 0, "limit_bn": 1}
    doc = {"day": "d", "ran_at": "t", "data_as_of": "a", "pnl_as_of_min": "a",
           "brief": {"headline": "h", "interpretation": [], "todo": [], "source": "CODE"},
           "total": u, "by_asset": {"bond": u}, "alerts_open": [], "stress": [], "shock": {}, "gaps": [],
           "validation": {"warnings": [], "errors": []}, "suggested_triggers": []}
    assert "자동 종결 보류" not in daily.to_markdown(doc)
    md = daily.to_markdown({**doc, "degraded": ["Refinitiv 미연결"]})
    assert "> 데이터 저하 — Refinitiv 미연결 → 경고 자동 종결·하향 보류" in md


# ── 자체검증 비교: 같은 포지션 조정안 여러 건 ──
def test_compare_checks_duplicate_position_ids_not_dropped():
    """같은 포지션에 조정안 2건(축소+헤지)이면 둘 다 비교 — 순서에 따라 정반대가 일치로 바뀌면 안 된다."""
    def a(pid, d):
        return {"position_id": pid, "direction": d}
    p = {"risk_grade": "WARN", "adjustments": [a("E1", "REDUCE"), a("E1", "HEDGE_UP")]}
    v = {"risk_grade": "WARN", "adjustments": [a("E1", "INCREASE"), a("E1", "HEDGE_UP")]}
    assert checks.compare_checks(p, v)["verdict"] == "DISAGREE"
    p_rev = {**p, "adjustments": p["adjustments"][::-1]}
    assert checks.compare_checks(p_rev, v)["verdict"] == "DISAGREE"
    same = {"risk_grade": "WARN", "adjustments": [a("E1", "HEDGE_UP"), a("E1", "REDUCE")]}
    assert checks.compare_checks(p, same)["verdict"] == "AGREE"
