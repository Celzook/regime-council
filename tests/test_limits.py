# -*- coding: utf-8 -*-
"""손실한도 엔진(limits.py) 계약 테스트 — 합성 한도(_LIM)·합성 포지션, data/ 실파일에 의존하지 않는다."""
import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import config  # noqa: E402
import limits  # noqa: E402

TODAY = datetime.date.today().isoformat()

# 필수 키(REQUIRED_LIMIT_KEYS)를 모두 갖춘 최소 합성 한도표
_LIM = {
    "total_limit_bn": 300, "asset_limits_bn": {"bond": 80, "equity": 160, "fx": 40},
    "stages": {"warn_pct": 50, "reduce_pct": 75, "halt_pct": 100},
    "stop_loss": {"approach_ratio": 0.7, "default_pct": {"bond": -2, "equity": -10, "mixed": -2}, "fx_open_default_pct": -6},
    "stress_scenarios": [{"id": "S0", "name": "s", "usdkrw_pct": -4, "sizing": True}],
    "deadlines": {"warn_response": "당일", "reduce_days": 3},
}


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    """is_sample·로더가 실제 data/ 를 읽지 않게."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(agent, "LOAD_ERRORS", {})


def _eq(pid, ccy="KRW", **kw):
    return {"id": pid, "name": pid, "asset": "equity", "mv_bn": 100, "pnl_ytd_bn": 0.0, "currency": ccy,
            "pnl_as_of": TODAY, **kw}


# ── 가격 미수신 → stale ──
def test_unpriced_legs_mark_position_stale():
    """가격 못 받은 다리가 있으면 기준일이 오늘이어도 stale + [데이터] 경고, 행에 unpriced_legs 전달."""
    doc = {"meta": {"as_of": TODAY}, "positions": [
        _eq("E1", unpriced_legs=[]),
        _eq("E2", ccy="USD", fx_exposure_bn=100, hedge_pct=0, pnl_fx_ytd_bn=0.0, unpriced_legs=["KRW=X"]),
        _eq("E3", pnl_as_of=None, unpriced_legs=["069500.KS"]),
    ]}
    ev = limits.evaluate(positions_doc=doc, limits=_LIM)
    rows = {r["id"]: r for r in ev["positions"]}
    assert rows["E1"]["stale"] is False and rows["E1"]["unpriced_legs"] == []
    assert rows["E2"]["stale"] is True and rows["E2"]["unpriced_legs"] == ["KRW=X"]
    assert rows["E3"]["stale"] is True
    assert sum("가격 미수신" in w for w in ev["validation"]["warnings"]) == 2


def test_old_positions_without_field_unchanged():
    """unpriced_legs 필드가 없는 예전 positions.json 은 예전대로(기준일만으로) 판정."""
    ev = limits.evaluate(positions_doc={"meta": {"as_of": TODAY}, "positions": [_eq("E1")]}, limits=_LIM)
    assert ev["positions"][0]["stale"] is False and ev["positions"][0]["unpriced_legs"] == []


# ── 엔 등 USD 아닌 환 오버레이: USD/KRW 대용 공시 ──
def _fx(ccy):
    return {"id": "F2", "name": "엔 롱", "asset": "fx", "currency": ccy, "mv_bn": 0, "fx_exposure_bn": 100,
            "hedge_pct": 0, "pnl_fx_ytd_bn": 0, "pnl_as_of": TODAY}


def test_non_usd_fx_stress_proxy_is_disclosed():
    """USD 아닌 환오픈분은 스트레스에서 USD/KRW 충격을 대용한다는 경고가 붙는다(USD 는 안 붙음)."""
    res = limits.evaluate(positions_doc={"meta": {"as_of": TODAY}, "positions": [_fx("JPY")]}, limits=_LIM)
    assert any("JPY" in w and "usdkrw_pct" in w for w in res["validation"]["warnings"])
    res = limits.evaluate(positions_doc={"meta": {"as_of": TODAY}, "positions": [_fx("USD")]}, limits=_LIM)
    assert not any("usdkrw_pct" in w for w in res["validation"]["warnings"])


def test_fx_stress_uses_net_but_base_stays_gross():
    """교차 오버레이(그로스 260·순 -2.6): 손절·한도 기준은 그로스, 스트레스만 순노출 크기·방향."""
    p = {**_fx("USD"), "fx_exposure_bn": 260, "fx_net_bn": -2.6, "fx_stop_pct": -3, "pnl_fx_ytd_bn": -2.6}
    leg = limits.expand_legs([p])[0]
    assert leg["mv_bn"] == 260
    assert limits.leg_stress_loss(leg, {"usdkrw_pct": -4}) == pytest.approx(-0.104)
    res = limits.evaluate(positions_doc={"meta": {"as_of": TODAY}, "positions": [p]}, limits=_LIM)
    assert res["positions"][0]["fx"]["stage"] == "NORMAL"
    # fx_net_bn 없는 예전 JSON 은 그로스·롱 그대로
    assert limits.leg_stress_loss(limits.expand_legs([_fx("USD")])[0], {"usdkrw_pct": -4}) == pytest.approx(4.0)


# ── 손익 분할 추정 표시(is None 기준 통일) ──
def _cb(split):
    return {"id": "B5", "name": "CB", "asset": "mixed", "mv_bn": 100, "pnl_ytd_bn": 0.0, "pnl_split": split,
            "look_through": {"equity_pct": 50}, "currency": "KRW"}


def test_pnl_split_zero_is_reported_not_estimated():
    """손익 0.0 분할은 보고값 — 다리·행 표시가 같아야 한다."""
    res = limits.evaluate(positions_doc={"meta": {}, "positions": [_cb({"equity": 0.0, "bond": 0.0})]}, limits=_LIM)
    assert res["positions"][0]["pnl_split_estimated"] is False
    assert not any("추정 분할" in w for w in res["validation"]["warnings"])
    res = limits.evaluate(positions_doc={"meta": {}, "positions": [_cb({"equity": 1.0})]}, limits=_LIM)
    assert res["positions"][0]["pnl_split_estimated"] is True
    assert any("추정 분할" in w for w in res["validation"]["warnings"])


# ── is_sample 이 로더 폴백을 그대로 반영 ──
def test_is_sample_true_when_loader_falls_back(tmp_path):
    """limits.json 이 깨졌거나 필수 키가 없으면 로더가 가안을 쓰므로 is_sample 도 True."""
    (tmp_path / "limits.sample.json").write_text(json.dumps({**_LIM, "meta": {}}), encoding="utf-8")
    lj = tmp_path / "limits.json"
    assert limits.is_sample("limits") is True                       # 파일 없음
    lj.write_text(json.dumps({"total_limit_bn": 1}), encoding="utf-8")
    assert limits.is_sample("limits") is True                       # 필수 키 누락
    lj.write_text("{", encoding="utf-8")
    assert limits.is_sample("limits") is True                       # JSON 오류
    assert "읽기 실패" in limits.load_limits()["meta"]["load_warning"]
    lj.write_text(json.dumps(_LIM), encoding="utf-8")
    assert limits.is_sample("limits") is False
    (tmp_path / "positions.json").write_text("{", encoding="utf-8")
    assert limits.is_sample("positions") is True
    (tmp_path / "positions.json").write_text(json.dumps({"meta": {}, "positions": []}), encoding="utf-8")
    assert limits.is_sample("positions") is False
