# -*- coding: utf-8 -*-
"""정량 분석(quant.py) 정의 회귀 테스트 — 네트워크·Refinitiv 없이 합성 데이터로 돈다.

손절·목표 장벽의 기준(진입 vs 오늘), 남은 손실한도 기준 초과 확률, 가격 이력이 없을 때의 빈 결과,
시험 포트 판정 한 곳, 백분위 정의(mid-rank) 통일을 고정한다.
실행: python -m pytest tests
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analytics  # noqa: E402
import equity  # noqa: E402
import quant  # noqa: E402


def _px(n: int = 300) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=n)
    r = np.random.default_rng(3).normal(0.0004, 0.01, n)
    return pd.DataFrame({"069500.KS": 100 * np.cumprod(1 + r)}, index=idx)


def _pos(mv: float, base: float, pnl: float) -> dict:
    return {"id": "E1", "name": "KODEX 200 롱", "asset": "equity", "mv_bn": mv, "base_bn": base, "pnl_ytd_bn": pnl,
            "stop_loss_pct": -10, "target_pct": 20,
            "legs": [{"name": "KODEX 200", "code": "069500", "market": "KRX ETF", "side": "롱", "mv_bn": mv}]}


def _flat_paths(monkeypatch, x: float):
    monkeypatch.setattr(quant, "vol_paths", lambda r, h, seed=7: (np.full((1, h), x), 0.01))
    monkeypatch.setattr(quant, "analog_paths", lambda r, h, vix: (np.full((1, h), x), [], []))


# ── A. 손절·목표 장벽 = 진입 기준 → 오늘 기준으로 이동 ──
def test_barrier_shifted_by_pnl_since_entry(monkeypatch):
    """손절·목표는 진입 기준 — 이미 +10억 이익이면 오늘 기준 장벽은 더 멀다."""
    _flat_paths(monkeypatch, -0.12)            # 오늘부터 −12% — 진입 기준 −10% 로 보면 손절, 실제로는 진입 대비 −1.2억
    res = quant.analyze_position(_pos(mv=110, base=100, pnl=10), _px(), None)
    assert res["stop_from_now_pct"] == pytest.approx(-18.18, abs=0.01)     # (−10 − 10) / 110
    assert res["target_from_now_pct"] == pytest.approx(9.09, abs=0.01)     # (20 − 10) / 110
    assert res["vol"]["1M"]["p_stop"] == 0
    assert res["analog"]["6M"]["p_stop"] == 0


def test_barrier_already_crossed_counts_as_hit(monkeypatch):
    """진입 대비 이미 −12%(손절 −10% 넘음)면 오늘 조금만 빠져도 손절 — 첫날 닿은 것으로 센다."""
    _flat_paths(monkeypatch, -0.01)
    res = quant.analyze_position(_pos(mv=88, base=100, pnl=-12), _px(), None)
    assert res["stop_from_now_pct"] > 0
    assert res["vol"]["1M"]["p_stop"] == 100
    assert res["vol"]["1M"]["p_target"] == 0


# ── B. 포트 한도 초과 확률 = 남은 한도(한도 + 현재 손익) 기준 ──
def _port_inputs(n: int = 400):
    idx = pd.bdate_range("2023-01-02", periods=n)
    rng = np.random.default_rng(5)
    series = {"A": pd.Series(rng.normal(0, 0.01, n), index=idx)}
    walk = lambda mu, sd: 100 * np.cumprod(1 + rng.normal(mu, sd, n))   # noqa: E731
    panel = pd.DataFrame({"kospi": walk(0.0003, 0.01), "vix": 15 + rng.normal(0, 2, n),
                          "ktb_3y": 3 + np.cumsum(rng.normal(0, 0.02, n)), "usdkrw": walk(0, 0.004)}, index=idx)
    return [{"id": "A", "base_bn": 100}], series, panel


def test_portfolio_breach_uses_remaining_room():
    """이미 쓴 손실은 남은 한도를 줄인다."""
    results, series, panel = _port_inputs()
    p0 = quant.analyze_portfolio(results, series, 50.0, panel, pnl_now_bn=0.0)
    p45 = quant.analyze_portfolio(results, series, 50.0, panel, pnl_now_bn=-45.0)
    p60 = quant.analyze_portfolio(results, series, 50.0, panel, pnl_now_bn=-60.0)
    assert p0["vol"]["p_breach"] < 5
    assert p45["room_bn"] == 5.0 and p45["vol"]["p_breach"] > 50
    assert p60["vol"]["p_breach"] == 100
    assert all(k == 0 for k in p60["vol"]["scale_for_breach"].values())
    assert p0["vol"]["scale_for_breach"]["20"] > p45["vol"]["scale_for_breach"]["20"]
    if "p_breach" in p60["analog"]:
        assert p60["analog"]["p_breach"] == 100


def test_portfolio_reduce_stage_probability():
    """축소 단계(한도 75%)는 한도 초과보다 먼저 닿는다 — 남은 축소 여유 = 한도 × 75% + 현재 손익."""
    results, series, panel = _port_inputs()
    p = quant.analyze_portfolio(results, series, 50.0, panel, pnl_now_bn=-30.0)
    assert p["room_reduce_bn"] == pytest.approx(7.5)
    assert p["vol"]["p_reduce"] > p["vol"]["p_breach"]


# ── C. 가격 이력이 없으면 예외 없이 NO_DATA ──
def test_portfolio_without_series_does_not_raise():
    """가격 이력이 하나도 없으면 빈 결과(NO_DATA) — 예외 없이."""
    out = quant.analyze_portfolio([], {}, 300.0, pd.DataFrame())
    assert out["n_positions"] == 0 and "NO_DATA" in out["analog"]["flags"]
    idx = pd.bdate_range("2024-01-01", periods=50)
    out2 = quant.analyze_portfolio([{"id": "A", "base_bn": 100}], {"A": pd.Series(np.nan, index=idx)}, 300.0, pd.DataFrame())
    assert out2["n_positions"] == 0
    md = quant.to_markdown({"generated_at": "2026-10-26T16:30:00", "price_last": None, "positions": [],
                            "portfolio": out, "scanner": []})
    assert "가격 이력 없음" in md


def test_position_returns_empty_column_is_none():
    """다리 가격 열이 비어 있으면 None — 비교 오류로 멈추지 않는다."""
    px = pd.DataFrame({"069500.KS": [np.nan] * 5}, index=pd.bdate_range("2024-01-01", periods=5))
    assert quant.position_returns(_pos(mv=100, base=100, pnl=0), px) is None


# ── D. 시험 포트 판정은 한 곳 ──
def test_is_trial_flag_or_note():
    """명시 플래그 또는 예전 비고 문구로 시험 포트 — 샘플 템플릿 비고는 시험 포트가 아님."""
    assert quant.is_trial({"is_trial": True})
    assert quant.is_trial({"note": "가상 시험 포트(Active 1조) — 실제 투자 아님"})
    assert not quant.is_trial({"note": "예시 템플릿 — 시험 포트(가상) 형식을 그대로 담은 것."})
    assert not quant.is_trial({})


# ── F. 백분위 = mid-rank(valhist.position 과 같은 정의) ──
def test_pctile_midrank_on_flat_series():
    """변하지 않는 기준금리는 백분위 50% — 100%(극단)로 읽히면 안 된다."""
    idx = pd.bdate_range("2020-01-01", periods=6 * 260)
    df = pd.DataFrame({"bok_base": 3.0}, index=idx)
    assert analytics.stats("bok_base", df)["pctile"] == 50
    assert equity.level_stats(pd.Series(20.0, index=pd.bdate_range("2024-01-01", periods=300)))["pct_1y"] == 50


# ── R19. 포지션 구성 지문을 quant.json 에 그대로 남긴다(시가 재평가로 정량 규칙이 빠지지 않게) ──
def test_run_copies_positions_key(monkeypatch):
    """quant.run 은 positions meta 의 positions_key 를 결과에 복사 — strategy 가 as_of 대신 이것으로 비교."""
    import limits
    doc = {"meta": {"as_of": "2026-10-26", "positions_key": "abc123"}, "positions": []}
    monkeypatch.setattr(limits, "load_positions", lambda: doc)
    monkeypatch.setattr(limits, "load_limits", lambda: {"total_limit_bn": 300})
    monkeypatch.setattr(limits, "evaluate", lambda **k: {"total": {"pnl_bn": 0.0}})
    monkeypatch.setattr(limits, "is_sample", lambda n: False)
    monkeypatch.setattr(quant, "refresh_prices", lambda syms, full=False: pd.DataFrame())
    monkeypatch.setattr(analytics, "load_panel", lambda: pd.DataFrame())
    out = quant.run(save=False)
    assert out["positions_key"] == "abc123" and out["positions_as_of"] == "2026-10-26"
