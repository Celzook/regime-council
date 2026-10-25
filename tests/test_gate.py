# -*- coding: utf-8 -*-
"""외부 AI 전송 게이트 회귀 테스트 — config.SHARE_REFINITIV_DATA / SHARE_VENDOR_DATA 가 False 면
에이전트에게 보내는 어떤 컨텍스트에도 시장 수치가 들어가지 않아야 한다(합성 값·tmp_path, 네트워크 없음).

합성 값 9.8765 · 1987.65 · 98.765 · 4.321 · 12.345 · 6.543 은 실제 데이터에 나오지 않는 표지값이다.
키 이름(rates_kr.ktb_3y 등)으로는 검사하지 않는다 — prompts/common.md 에 이미 들어 있다.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import config  # noqa: E402
import daily  # noqa: E402
import history  # noqa: E402
import limits  # noqa: E402
import mp  # noqa: E402
import store  # noqa: E402
import voice  # noqa: E402

SENTINELS = ("9.8765", "1987.65", "98.765")
SHOCK_ALERT = {"code": "SHOCK", "grade": "WARN", "grade_ko": "경고", "bucket": "시장 급변",
               "message": "시장 급변 트리거: 국고10y 일변동 +98.765bp", "required_action": "점검",
               "due": "당일", "status": "OPEN", "days_open": 0}


def _ev() -> dict:
    """로컬 limits.json·portfolio.json 에 기대지 않게 sample 파일로 평가한다."""
    return limits.evaluate(positions_doc=json.loads((ROOT / "data/positions.sample.json").read_text(encoding="utf-8")),
                           limits=json.loads((ROOT / "data/limits.sample.json").read_text(encoding="utf-8")))


def _room_data(**extra) -> dict:
    d = {"ev": _ev(), "gaps": {}, "mps": {}, "pending": [], "alerts_open": [dict(SHOCK_ALERT)],
         "market": {"rates_kr": {"ktb_3y": {"value": 9.8765, "unit": "%", "as_of": "2026-10-24"}},
                    "fx": {"usdkrw": {"value": 1987.65, "unit": "", "as_of": "2026-10-24"}}},
         "shock": {"available": True, "as_of": "2026-10-24", "metrics": {"ktb_10y_daily_bp": 98.765},
                   "triggered": ["국고10y 일변동 +98.765bp"]}}
    d.update(extra)
    return d


@pytest.fixture
def refinitiv_off(monkeypatch):
    monkeypatch.setattr(config, "SHARE_REFINITIV_DATA", False)


@pytest.fixture
def refinitiv_on(monkeypatch):
    monkeypatch.setattr(config, "SHARE_REFINITIV_DATA", True)


# ── 공통 게이트(agent.py) ──
def test_share_gates_fail_closed_when_flag_missing(monkeypatch):
    """플래그가 config 에 없으면 '보내지 않음' — 예전 getattr(..., True) 처럼 열리면 안 된다."""
    monkeypatch.delattr(config, "SHARE_REFINITIV_DATA", raising=False)
    monkeypatch.delattr(config, "SHARE_VENDOR_DATA", raising=False)
    assert agent.share_refinitiv() is False
    assert agent.share_vendor() is False
    assert "98.765" not in agent.alert_msg(SHOCK_ALERT)


def test_alert_msg_keeps_non_shock_and_shares_when_allowed(refinitiv_on):
    assert agent.alert_msg(SHOCK_ALERT) == SHOCK_ALERT["message"]
    other = {"code": "UTIL_STAGE", "message": "채권 손실한도 소진율 71%"}
    assert agent.alert_msg(other) == other["message"]


# ── 에이전트 룸(voice.agent_context) ──
def test_agent_context_withholds_market_when_refinitiv_restricted(monkeypatch, refinitiv_off):
    ctx = voice.agent_context("fx", _room_data())
    for s in SENTINELS:
        assert s not in ctx
    assert "외부 전송 제한" in ctx
    # 같은 입력이 실제 경로를 지나는지 — 허용이면 세 표지값이 다 보인다
    monkeypatch.setattr(config, "SHARE_REFINITIV_DATA", True)
    ctx = voice.agent_context("fx", _room_data())
    for s in SENTINELS:
        assert s in ctx


def test_agent_context_labels_sample_market(refinitiv_on):
    ctx = voice.agent_context("bond", _room_data(market_sample=True))
    line = next(x for x in ctx.splitlines() if "담당 시장 스냅샷" in x)
    assert "예시값" in line


def test_gather_flags_corrupt_market_json_as_sample(monkeypatch, tmp_path):
    """market.json 이 깨져 sample 로 대체되면 market_sample=True (limits.is_sample 은 존재만 본다)."""
    monkeypatch.setattr(agent, "LOAD_ERRORS", {})   # 깨진 파일 오류 기록이 다른 테스트로 새지 않게
    (tmp_path / "market.json").write_text("{", encoding="utf-8")
    shutil.copy(ROOT / "data/market.sample.json", tmp_path / "market.sample.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(limits, "load_limits", lambda: json.loads((ROOT / "data/limits.sample.json").read_text(encoding="utf-8")))
    monkeypatch.setattr(store, "load_mp", lambda *a, **k: None)
    monkeypatch.setattr(store, "load_alerts", lambda: [])
    monkeypatch.setattr(store, "latest_daily", lambda: None)
    monkeypatch.setattr(store, "list_checks", lambda: [])
    d = voice.gather(_ev())
    assert d["market"] and d["market_sample"] is True


# ── 대화 화면 공유(voice.screen_block) ──
def test_screen_block_withholds_vendor_screen(monkeypatch, refinitiv_on):
    monkeypatch.setattr(config, "SHARE_VENDOR_DATA", False)
    out = voice.screen_block({"tab": "ref", "label": "매크로", "text": "카드채 9.8765"})
    assert "9.8765" not in out and "매크로" in out
    assert "9.8765" in voice.screen_block({"tab": "equity", "label": "주식", "text": "카드채 9.8765"})


def test_screen_block_withholds_every_tab_when_refinitiv_restricted(monkeypatch, refinitiv_off):
    monkeypatch.setattr(config, "SHARE_VENDOR_DATA", True)
    out = voice.screen_block({"tab": "limits", "label": "한도·로그", "text": "USD/KRW 1987.65"})
    assert "1987.65" not in out and "한도·로그" in out
    assert voice.screen_block({"tab": "limits", "label": "한도·로그", "text": ""}) == ""


# ── 일일 브리프 입력(daily._brief_ctx) ──
def _brief() -> str:
    shock = {"available": True, "metrics": {"ktb_10y_daily_bp": 98.765, "usdkrw_daily_pct": 4.321},
             "triggered": ["국고10y 일변동 +98.765bp"]}
    return daily._brief_ctx(_ev(), [dict(SHOCK_ALERT)], shock, [], [])


def test_brief_ctx_withholds_shock_metrics_when_restricted(refinitiv_off):
    ctx = _brief()
    assert "98.765" not in ctx and "4.321" not in ctx
    assert "트리거 1건" in ctx


def test_brief_ctx_shares_shock_metrics_when_allowed(refinitiv_on):
    ctx = _brief()
    assert "98.765" in ctx and "4.321" in ctx


# ── 공통 컨텍스트(agent.build_shared_context) ──
STRATEGY_D = {"generated_at": "2026-10-25T09:00", "summary": {"headline": "오늘 할 일 1건 — 먼저 A: 축소"},
              "portfolio": {"lines": ["변동성 기준 12.345%"], "concentration": []},
              "actions": [{"id": "P1", "name": "A", "action": "축소", "headline": "1σ 6.543%", "impact_bn": 1}],
              "ideas": [{"name": "X", "direction": "롱", "score": 77.0, "action": "검토"}], "rules": ["r"]}


def test_shared_context_restricted_has_no_market_values(monkeypatch, tmp_path, refinitiv_off):
    import strategy
    (tmp_path / "market.json").write_text(json.dumps(
        {"meta": {}, "rates_kr": {"ktb_3y": {"value": 9.8765, "unit": "%", "as_of": "2026-10-24"}}}), encoding="utf-8")
    shutil.copy(ROOT / "data/portfolio.sample.json", tmp_path / "portfolio.sample.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(strategy, "load", lambda **k: STRATEGY_D)
    ctx = agent.build_shared_context()
    for s in ("9.8765", "12.345", "6.543", "+77"):
        assert s not in ctx
    assert '"rates_kr": {' not in ctx
    assert "| P1 A | 축소 |" in ctx


# ── MP·점검 입력(과거 유사 국면) ──
def test_mp_context_withholds_analogs_when_restricted(monkeypatch, refinitiv_off):
    monkeypatch.setattr(history, "find_analogs", lambda asset: {
        "asset_name": "채권", "as_of": "2026-10-24", "search": {}, "flags": ["F1"],
        "current_features": {"ktb_10y": 9.8765}, "analogs": [], "summary": {}, "caveats": []})
    monkeypatch.setattr(store, "load_mp", lambda *a, **k: None)
    ctx, extra = mp.build_context("bond", _ev())
    assert "9.8765" not in ctx and "외부 전송 제한" in ctx
    assert extra["analog_flags"] == ["F1"]          # 코드 판단용 플래그는 그대로
    monkeypatch.setattr(config, "SHARE_REFINITIV_DATA", True)
    assert "9.8765" in mp.build_context("bond", _ev())[0]


# ── 점검(checks.run_check) — Agent 를 가짜로 바꿔 에이전트로 가는 입력만 본다 ──
def _stub_check(monkeypatch, tmp_path) -> list:
    import checks
    seen = []

    class FakeAgent:
        def __init__(self, *a, **k):
            pass

        def opinion(self, instruction, *a, **k):
            seen.append(instruction)
            return None

    ev = _ev()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(checks.anthropic, "Anthropic", lambda *a, **k: None)
    monkeypatch.setattr(checks, "Agent", FakeAgent)
    monkeypatch.setattr(checks, "build_shared_context", lambda: "")
    monkeypatch.setattr(limits, "evaluate", lambda **k: ev)
    monkeypatch.setattr(history, "find_analogs", lambda asset: {
        "asset_name": "채권", "as_of": "2026-10-24", "search": {}, "flags": ["F1"],
        "current_features": {"ktb_10y": 9.8765}, "analogs": [], "summary": {}, "caveats": []})
    monkeypatch.setattr(store, "load_mp", lambda *a, **k: None)
    monkeypatch.setattr(store, "new_id", lambda *a, **k: "CHK-TEST-001")
    monkeypatch.setattr(store, "save_check", lambda doc: None)
    monkeypatch.setattr(store, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path)
    return seen


def test_check_context_withholds_analogs_when_restricted(monkeypatch, tmp_path, refinitiv_off):
    import checks
    seen = _stub_check(monkeypatch, tmp_path)
    checks.run_check("SHOCK", "bond")
    assert len(seen) == 2 and all("9.8765" not in s and "외부 전송 제한" in s for s in seen)
    monkeypatch.setattr(config, "SHARE_REFINITIV_DATA", True)
    seen.clear()
    checks.run_check("SHOCK", "bond")
    assert all("9.8765" in s for s in seen)


def test_template_shock_action_note_withheld_when_restricted(monkeypatch, refinitiv_off):
    """템플릿 카드의 '시장 급변 점검 실행' 메모는 점검 에이전트 입력(운용역 메모)으로 간다."""
    act = next(a for a in voice.template_voice("bond", _room_data())["proposed_actions"] if a["trigger"] == "SHOCK")
    assert "98.765" not in act["note"] and "1건" in act["note"]
    monkeypatch.setattr(config, "SHARE_REFINITIV_DATA", True)
    act = next(a for a in voice.template_voice("bond", _room_data())["proposed_actions"] if a["trigger"] == "SHOCK")
    assert "98.765" in act["note"]


def test_room_shock_action_drops_client_note_when_restricted(monkeypatch, tmp_path, refinitiv_off):
    """화면이 보낸 급변 메모(수치 포함)를 /api/room/action 이 점검 에이전트로 넘기지 않는다."""
    import web
    seen = _stub_check(monkeypatch, tmp_path)
    monkeypatch.setattr(web, "start_job", lambda kind, fn, *a, **k: (fn(*a, **k), "job-1")[1])
    r = web.app.test_client().post("/api/room/action", json={"type": "check", "trigger": "SHOCK", "asset": "bond",
                                                              "note": "국고10y 일변동 +98.765bp"},
                                   environ_base={"REMOTE_ADDR": "127.0.0.1"}, headers={"Host": "127.0.0.1:5101"})
    assert r.status_code == 200
    assert len(seen) == 2 and all("98.765" not in s for s in seen)


# ── 전략 보드(제한 모드 요약)·인포맥스 게이트 ──
def test_restricted_strategy_md_keeps_sample_tag(monkeypatch, refinitiv_off):
    import strategy
    monkeypatch.setattr(strategy, "load", lambda **k: dict(STRATEGY_D, is_sample=True))
    assert "샘플(계획치)" in agent._strategy_md(restricted=True)


def test_manual_inputs_vendor_gate_fails_closed(monkeypatch, tmp_path):
    """SHARE_VENDOR_DATA 가 config 에 없으면 브리지 스왑포인트를 쓰지 않는다(오류로 죽지도 않는다)."""
    import analytics
    monkeypatch.delattr(config, "SHARE_VENDOR_DATA", raising=False)
    (tmp_path / "macro_live.json").write_text(json.dumps({"meta": {"read_at": "2026-10-24T09:00"}, "series": [
        {"key": "swap_3m", "status": "OK", "value": 98.765, "date": "2026-10-24"}]}), encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert "swap_point_3m" not in analytics.manual_inputs()
    monkeypatch.setattr(config, "SHARE_VENDOR_DATA", True, raising=False)
    assert analytics.manual_inputs()["swap_point_3m"]["value"] == 98.765
