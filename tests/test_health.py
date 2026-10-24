# -*- coding: utf-8 -*-
"""데이터 이상 점검(datahealth) 회귀 테스트 — 네트워크·실데이터 없이 합성 시계열로 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datahealth as H  # noqa: E402

D = pd.bdate_range("2025-01-01", periods=400)


def _walk(seed=0, start=100.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(start * np.exp(np.cumsum(rng.normal(0, 0.01, len(D)))), index=D)


def _kinds(s, rate=False):
    return [k for k, _ in H.check_series(s, rate)]


def test_clean_series_has_no_issue():
    assert _kinds(_walk()) == []
    assert H.check_stale({("yf", "sp500"): D[-1], ("equity", "SPY"): D[-1], ("kofia", "ktb_3y"): D[-1], ("yf", "kospi"): D[-1]}) == []


def test_jump_detected_in_pct_and_bp():
    s = _walk()
    s.iloc[-1] = s.iloc[-2] * 1.25                     # 하루 +25% — 1% 변동 시계열에선 이상
    assert _kinds(s) == ["jump"]
    r = pd.Series(3.0 + np.cumsum(np.random.default_rng(1).normal(0, 0.03, len(D))), index=D)
    r.iloc[-1] = r.iloc[-2] + 1.5                      # 금리 +150bp
    assert _kinds(r, rate=True) == ["jump"]


def test_flat_detected_but_step_series_skipped():
    s = _walk()
    s.iloc[-6:] = s.iloc[-6]                           # 6거래일 같은 값(정지 시세)
    assert _kinds(s) == ["flat"]
    base = pd.Series(3.0, index=D)                     # 기준금리처럼 원래 거의 안 움직이는 시계열
    base.iloc[200:] = 2.75
    assert "flat" not in _kinds(base, rate=True)
    s2 = _walk()
    s2.iloc[-6:] = s2.iloc[-6]
    assert "flat" not in [k for k, _ in H.check_series(s2, False, flat_run=8)]


def test_stale_within_market_group():
    last = {("yf", "kospi"): D[-1], ("kofia", "ktb_3y"): D[-1], ("equity", "005930.KS"): D[-4],
            ("yf", "sp500"): D[-2], ("equity", "SPY"): D[-2],      # 미국은 하루 늦어도 자기 묶음 안에선 최신
            ("yf", "ks200_fut"): D[-2]}
    out = {(f, c): sev for f, c, sev, _ in H.check_stale(last)}
    assert out == {("equity", "005930.KS"): "warn", ("yf", "ks200_fut"): "info"}


def test_stale_reference_ignores_single_early_value():
    """장중에 받은 값 하나(VIX)가 묶음 기준일을 끌어올려 나머지를 stale 로 만들지 않는다."""
    last = {("yf", "vix"): D[-1], ("yf", "sp500"): D[-3], ("equity", "SPY"): D[-3], ("fred", "us_hy_oas"): D[-4]}
    assert H.check_stale(last) == []
