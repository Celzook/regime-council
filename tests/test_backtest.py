# -*- coding: utf-8 -*-
"""백테스트 계산 회귀 테스트 — Refinitiv·네트워크·data/ 없이 합성 데이터로 돈다.

ELB 발행 비용이 투자자에게 돌아가지 않게, QIS 초과수익은 현지 현금 기준, 부가 지표도 종료일에서 끝나게,
데이터 파일이 없으면 500 대신 고칠 방법을 알려 주게 고정한다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest  # noqa: E402


# ── ELB: 평평한 지수면 만기 = 원금(발행 비용은 돌려받지 않음) ──
def test_elb_flat_index_returns_principal_not_fee(monkeypatch):
    idx = pd.bdate_range("2020-12-01", "2023-01-31")
    S = pd.Series(100.0, idx)
    monkeypatch.setattr(backtest, "_s", lambda t: S)
    monkeypatch.setattr(backtest, "_iv", lambda k: pd.Series(0.2, idx))
    monkeypatch.setattr(backtest, "cash", lambda ccy, i: pd.Series(0.03 / 252, i))
    ret, _, _ = backtest.s_elb({"under": "kospi200", "fee": 0.5}, pd.Timestamp("2022-01-01"), idx[-1])
    w = ret[(ret.index > "2022-01-03") & (ret.index <= "2023-01-02")]   # 2022 발행분: 2022-01-03 → 만기 2023-01-02
    assert (1 + w).prod() == pytest.approx(1.0, abs=1e-9)                 # 고치기 전 ≈ 1/(1 − 0.5%) = 1.005
    assert abs(w.iloc[0]) < 0.001                                         # 첫날 급락·급등 없이 고르게 상각


def test_elb_zero_participation_no_first_day_jump(monkeypatch):
    idx = pd.bdate_range("2020-12-01", "2023-01-31")
    monkeypatch.setattr(backtest, "_s", lambda t: pd.Series(100.0, idx))
    monkeypatch.setattr(backtest, "_iv", lambda k: pd.Series(0.2, idx))
    monkeypatch.setattr(backtest, "cash", lambda ccy, i: pd.Series(0.001 / 252, i))   # 금리 0.1% < 비용 0.5% → 참여율 0
    ret, _, _ = backtest.s_elb({"under": "kospi200", "fee": 0.5}, pd.Timestamp("2022-01-01"), idx[-1])
    w = ret[(ret.index > "2022-01-03") & (ret.index <= "2023-01-02")]
    assert abs(w.iloc[0]) < 0.001                                         # 고치기 전 첫날 ≈ +0.4% 급등
    assert (1 + w[w.index >= "2022-01-10"]).prod() == pytest.approx(1.0, abs=1e-6)   # 고치기 전 ≈ 0.996(1년 내내 하락)


def test_elb_extras_skip_roll_on_end_date(monkeypatch):
    idx = pd.bdate_range("2020-12-01", "2023-01-31")
    monkeypatch.setattr(backtest, "_s", lambda t: pd.Series(100.0, idx))
    monkeypatch.setattr(backtest, "_iv", lambda k: pd.Series(0.2, idx))
    monkeypatch.setattr(backtest, "cash", lambda ccy, i: pd.Series(np.where(i < "2023-01-01", 0.03, 0.001) / 252, i))
    p, start = {"under": "kospi200", "fee": 0.5}, pd.Timestamp("2022-01-03")
    _, _, e1 = backtest.s_elb(p, start, pd.Timestamp("2022-12-30"))
    _, _, e2 = backtest.s_elb(p, start, pd.Timestamp("2023-01-02"))   # 종료일 = 2023 발행일(참여율 0) — 기간에 수익일 없음
    assert e2["평균 참여율"] == e1["평균 참여율"]                       # 고치기 전엔 2023 발행분(0%)까지 평균해 절반


# ── QIS: 지수가 현지 현금만큼만 오르면(초과수익 0) 전략 = 미국 현금 ──
def test_qis_excess_uses_local_cash(monkeypatch):
    idx = pd.bdate_range("2020-01-01", periods=600)
    rates = {"sp500": 0.05, "kospi200": 0.03, "nikkei": 0.0, "sx5e": 0.0}
    df = pd.DataFrame({k: (1 + v / 252) ** np.arange(len(idx)) for k, v in rates.items()}, index=idx)
    monkeypatch.setattr(backtest, "_tr_index", lambda keys: df[keys])
    monkeypatch.setattr(backtest, "tr_ret", lambda k: df[k].pct_change().dropna())
    monkeypatch.setattr(backtest, "cash", lambda ccy, i: pd.Series({"USD": 0.05, "KRW": 0.03}.get(ccy, 0.0) / 252, i))
    ret, _, _ = backtest.s_qis({"look": 12, "fee": 0}, idx[300], idx[-1])
    out = ret.loc[idx[300]:]
    assert len(out) > 200
    assert list(out) == pytest.approx([0.05 / 252] * len(out))           # 고치기 전엔 KRW 다리에 미국 금리를 빼 낮았다


def test_cash_jpy_eur_zero():
    D = pd.bdate_range("2024-01-01", periods=10)
    for ccy in ("JPY", "EUR"):                                            # 파일 없이 0% — 고치기 전엔 ^IRX 를 찾다 실패
        assert backtest.cash(ccy, D).eq(0).all()


# ── 부가 지표도 화면 기간(start, end] 안에서만 ──
def test_extras_stop_at_end_date(monkeypatch):
    idx = pd.bdate_range("2020-01-01", periods=600)
    r = pd.Series(np.r_[np.full(300, 0.002), np.full(300, -0.004)], idx)   # 앞 절반 상승, 뒤 절반 하락
    monkeypatch.setattr(backtest, "tr_ret", lambda k: r)
    monkeypatch.setattr(backtest, "cash", lambda ccy, i: pd.Series(0.0, i))
    _, _, extra = backtest.s_tactical({"under": "sp500", "ma": 60, "down": "cash"}, idx[100], idx[280])
    assert extra["평균 노출"] == "100%"                                   # 종료일 뒤 하락 구간(노출 0%)이 섞이지 않음
    assert extra["노출 변경 횟수"] == "0회"


# ── 데이터 파일 없음 → 500 대신 고칠 방법 ──
def test_backtest_missing_history_is_friendly(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "HIST", tmp_path / "none.csv")
    monkeypatch.setattr(backtest, "_C", {})
    with pytest.raises(ValueError, match="다시 받기"):
        backtest.run("EQ-FUT-01")


def test_krw_cash_missing_kofia_is_friendly(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "KOFIA", tmp_path / "none.csv")
    with pytest.raises(ValueError, match="CD 91일"):
        backtest.cash("KRW", pd.bdate_range("2024-01-01", periods=5))


def test_catalog_route_missing_history_is_503(tmp_path, monkeypatch):
    import web
    monkeypatch.setattr(backtest, "HIST", tmp_path / "none.csv")
    monkeypatch.setattr(backtest, "_C", {})
    r = web.app.test_client().get("/api/backtest/catalog", headers={"Host": "127.0.0.1:5101"})
    assert r.status_code == 503
    assert "다시 받기" in r.get_json()["error"]
