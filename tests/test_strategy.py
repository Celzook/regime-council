# -*- coding: utf-8 -*-
"""전략 보드(strategy.py) 규칙 회귀 테스트 — 네트워크 없이 합성 입력과 추적되는 *.sample.json 으로 돈다.

할 일 우선순위(시장 신호 포함 후 정렬), 이미 숏 보유한 하락 신호, 통화와 무관한 노출 기준,
샘플·시험 포트 표시, 오래된 quant.json 제외를 고정한다.
실행: python -m pytest tests
"""
import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import limits  # noqa: E402
import quant  # noqa: E402
import strategy  # noqa: E402

SAMPLE = json.loads((Path(__file__).resolve().parents[1] / "data" / "positions.sample.json").read_text(encoding="utf-8"))


def _eq_pos() -> dict:
    return {"id": "E1", "name": "삼성전자 롱", "asset": "equity", "strategy": "마켓타이밍", "base_bn": 100, "mv_bn": 100,
            "pnl_ytd_bn": 0, "stop_loss_pct": -10, "target_pct": 20,
            "legs": [{"name": "삼성전자", "code": "005930", "side": "롱", "mv_bn": 100, "beta": 1, "qty": 1000}]}


_EV = {"positions": [{"id": "E1", "stop_ratio_pct": 0, "approach_ratio_pct": 70}],
       "by_asset": {"equity": {"stop_budget_free_bn": 500}}}


def _q(tech: float, v1: dict) -> dict:
    return {"positions": [{"id": "E1", "tech": {"score": tech, "label": "-"}, "vol": {"1M": v1},
                           "analog": {"6M": {"p_target": 60, "p_stop": 10}}}],
            "scanner": [{"key": "kospi", "name": "KOSPI", "score": -50}], "portfolio": {}}


# ── E1. 시장 신호 논리 점검까지 붙인 뒤 정렬 ──
def test_market_against_outranks_add():
    """시장 신호가 반대면 '확대 검토'보다 '논리 점검'이 먼저."""
    out = strategy._position_rows({"positions": [_eq_pos()]}, _EV, _q(40, {}))
    assert out[0]["action"] == "논리 점검"
    assert "확대 검토" in [a["action"] for a in out[0]["also"]]


def test_logic_check_not_duplicated():
    """추세 기준 논리 점검과 시장 신호 논리 점검이 함께 걸리면 한 줄로 합친다."""
    out = strategy._position_rows({"positions": [_eq_pos()]}, _EV, _q(-40, {"p_stop": 60, "p_target": 5}))
    assert out[0]["action"] == "논리 점검"
    assert "논리 점검" not in [a["action"] for a in out[0]["also"]]
    assert "시장 신호" in out[0]["headline"] and "손절 먼저 확률" in out[0]["headline"]


# ── E2. 이미 숏으로 보유한 하락 신호 ──
def test_idea_already_short_is_hold():
    """이미 숏으로 보유한 하락 신호는 '숏 검토'가 아니라 '보유 중'."""
    q = {"scanner": [{"key": "ktb_10y", "name": "국고10년", "score": -45, "label": "매우 약세",
                      "analog_6m_median_pct": -1.0, "analog_6m_up_pct": 40}]}
    doc = {"positions": [{"id": "B2", "name": "국채선물 숏", "asset": "bond", "base_bn": 100, "mv_bn": 100,
                          "legs": [{"name": "10년 국채선물", "code": "LKTB F 2612", "side": "숏", "mv_bn": 90}]}]}
    idea = strategy._ideas(q, doc, {"by_asset": {}})[0]
    assert idea["action"] == "보유 중 — 유지·확대 검토"
    assert "보유 중" in idea["say"]


# ── E3. 노출 기준은 통화와 무관 ──
def test_exposure_base_independent_of_currency():
    """통화만 다른 같은 포지션은 노출 판정이 같아야."""
    legs = [{"name": "삼성전자", "code": "005930", "side": "롱", "mv_bn": 100, "beta": 1},
            {"name": "KOSPI200 선물", "code": "KOSPI200 F 2612", "side": "숏", "mv_bn": 85}]
    krw = {"id": "K", "name": "원화", "asset": "equity", "base_bn": 120, "mv_bn": 100, "legs": legs}
    usd = {**krw, "id": "U", "name": "달러", "currency": "USD", "fx_exposure_bn": 100}
    ids = {h["id"] for h in strategy._exposures([krw, usd]).get("kospi", [])}
    assert ids in (set(), {"K", "U"})


# ── D·E4. build — 샘플 표시, 오래된 quant 제외 ──
@pytest.fixture
def sample_only(monkeypatch):
    """positions.json 이 있어도 무시하고 추적되는 positions.sample.json 으로."""
    real = limits.load_json
    monkeypatch.setattr(limits, "load_json", lambda p: None if Path(p).name == "positions.json" else real(p))


def test_build_flags_sample_positions(monkeypatch, sample_only):
    """positions.json 이 없으면 전략 보드도 샘플임을 표시."""
    monkeypatch.setattr(limits, "is_sample", lambda n: True)
    monkeypatch.setattr(quant, "load", lambda max_age_hours=None: None)
    d = strategy.build(save=False)
    assert d["is_sample"] is True and d["is_trial"] is False
    assert "샘플(계획치)" in strategy.to_markdown(d)


def _write_quant(tmp_path, monkeypatch, hours_ago: float, as_of: str | None) -> None:
    at = (datetime.datetime.now() - datetime.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    q = {"generated_at": at, "positions_as_of": as_of, "portfolio": {}, "scanner": [],
         "positions": [{"id": "E1", "tech": {"score": -60, "label": "매우 약세"}, "vol": {"1M": {"p_stop": 90, "p_target": 1}}, "analog": {}}]}
    f = tmp_path / "quant.json"
    f.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(quant, "OUT", f)


def _heads(d: dict) -> list[str]:
    return [a["headline"] for a in d["actions"]] + [x["headline"] for a in d["actions"] for x in a["also"]]


def test_stale_quant_is_ignored(tmp_path, monkeypatch, sample_only):
    """오래된 quant.json 은 규칙에 쓰지 않고 표시만."""
    as_of = SAMPLE["meta"]["as_of"]
    _write_quant(tmp_path, monkeypatch, 1, as_of)                  # 신선한 결과는 규칙에 쓰임(대조군)
    fresh = strategy.build(save=False)
    assert fresh["quant_at"] and fresh["quant_stale_at"] is None
    assert any("손절 먼저 확률" in h for h in _heads(fresh))

    _write_quant(tmp_path, monkeypatch, 5 * 24, as_of)             # 5일 전
    d = strategy.build(save=False)
    assert d["quant_stale_at"] is not None and d["quant_at"] is None
    assert not any("손절 먼저 확률" in h for h in _heads(d))
    assert "오래돼" in d["portfolio"]["lines"][0]


def test_quant_for_other_positions_date_is_ignored(tmp_path, monkeypatch, sample_only):
    """다른 포지션 기준일로 계산한 quant.json 은 포지션별 확률이 맞지 않으니 빼고 사유를 남긴다."""
    _write_quant(tmp_path, monkeypatch, 1, "1999-01-01")
    d = strategy.build(save=False)
    assert d["quant_at"] is None and "기준일" in (d["quant_stale_reason"] or "")


# ── R19. 시가 재평가(as_of 만 바뀜)로는 정량 규칙이 빠지지 않는다 — 구성 지문(positions_key) 비교 ──
def _fresh_q(tmp_path, monkeypatch, as_of: str, key: str | None) -> None:
    _write_quant(tmp_path, monkeypatch, 1, as_of)
    f = quant.OUT
    q = json.loads(f.read_text(encoding="utf-8"))
    if key is not None:
        q["positions_key"] = key
    f.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")


def test_quant_fresh_same_key_new_as_of(tmp_path, monkeypatch):
    """같은 구성(지문 같음)이면 기준일이 달라도 신선 — 지문이 한쪽에라도 없으면 예전처럼 기준일로 비교."""
    _fresh_q(tmp_path, monkeypatch, "2026-10-25", "K1")
    q, at, why = strategy._quant_fresh({"as_of": "2026-10-26", "positions_key": "K1"})
    assert q is not None and at is None and why is None
    q, at, why = strategy._quant_fresh({"as_of": "2026-10-26"})                     # 포지션 쪽 지문 없음 → 기준일 비교
    assert q is None and "기준일" in why
    q, _, why = strategy._quant_fresh({"as_of": "2026-10-25"})
    assert q is not None and why is None
    _fresh_q(tmp_path, monkeypatch, "2026-10-25", None)                              # quant 쪽 지문 없음(예전 파일) → 기준일 비교
    q, _, why = strategy._quant_fresh({"as_of": "2026-10-26", "positions_key": "K1"})
    assert q is None and "기준일" in why


def test_quant_other_composition_is_stale(tmp_path, monkeypatch, sample_only):
    """기준일이 같아도 구성 지문이 다르면 빼고, 안내 줄은 빠진 '규모 조정'과 '다시 계산' 버튼을 알려 준다. 80시간 검사는 그대로."""
    as_of = SAMPLE["meta"]["as_of"]
    _fresh_q(tmp_path, monkeypatch, as_of, "OLD")
    q, at, why = strategy._quant_fresh({"as_of": as_of, "positions_key": "NEW"})
    assert q is None and at and "구성" in why
    real = limits.load_positions
    monkeypatch.setattr(limits, "load_positions", lambda: (lambda d: {**d, "meta": {**d["meta"], "positions_key": "NEW"}})(real()))
    d = strategy.build(save=False)
    assert d["quant_at"] is None and "구성" in d["quant_stale_reason"]
    assert "규모 조정" in d["portfolio"]["lines"][0] and "다시 계산" in d["portfolio"]["lines"][0]
    _write_quant(tmp_path, monkeypatch, 5 * 24, as_of)                               # 지문이 같아도 오래되면 제외
    q, _, why = strategy._quant_fresh({"as_of": as_of, "positions_key": None})
    assert q is None and "오래돼" in why


# ── R20. 이미 한도 초과(남은 한도 ≤ 0) — 비율 조정 대신 '추가 위험 금지', 이중 마이너스 없음 ──
def test_portfolio_already_breached():
    q = {"portfolio": {"limit_bn": 300, "pnl_now_bn": -320, "room_bn": -20.0,
                       "vol": {"p_breach": 100, "p_reduce": 100, "scale_for_breach": {"10": 0.0, "20": 0.0, "30": 0.0}},
                       "analog": {"p_breach": 100}}}
    port = strategy._portfolio([], q, {"by_asset": {}, "stress": []}, {"positions": []})
    txt = " ".join(port["lines"] + [t["label"] for t in port["tiles"]] + [port["sub"]])
    assert "추가 위험 금지" in port["lines"][1] and "이미" in port["tiles"][0]["label"]
    assert port["tiles"][0]["value"] == "초과" and "새 위험은 더하지 말고" in port["sub"]
    assert "(-" not in txt and "−-" not in txt and "-20억" not in txt
