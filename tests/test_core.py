# -*- coding: utf-8 -*-
"""핵심 계산 회귀 테스트 — Refinitiv·네트워크 없이 돈다(합성 데이터).

2026-10-23 검증에서 확인하고 고친 문제가 다시 생기지 않게 고정한다:
벤치마크 날짜 정렬, 초과수익 샤프, 총수익 이력 잇기, VIX 빈칸 채우기, 티커→RIC 변환, 사내 공유 접속 차단.
실행: python -m pytest tests   (Windows 파이썬)
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest  # noqa: E402
import refinitiv_src as R  # noqa: E402

D = pd.bdate_range("2024-01-01", periods=10)


# ── 벤치마크 정렬(_align) ──
def test_align_compounds_missing_days():
    """전략 달력에 없는 날의 벤치마크 수익은 버리지 않고 다음 날로 복리 합산한다."""
    r = pd.Series(0.01, index=D)
    idx = D.delete(3)                     # 4번째 날이 빠진 전략 달력
    out = backtest._align(r, idx)
    assert out.index.equals(idx)
    assert out.iloc[3] == pytest.approx(1.01 ** 2 - 1)
    assert (1 + out).prod() == pytest.approx((1 + r).prod())


def test_align_first_day_uses_previous_level():
    r = pd.Series([0.01, 0.02, 0.03, 0.04], index=D[:4])
    out = backtest._align(r, D[2:4])
    assert out.iloc[0] == pytest.approx(0.03)
    assert out.iloc[1] == pytest.approx(0.04)


def test_align_empty_index():
    assert backtest._align(pd.Series(0.01, index=D), pd.DatetimeIndex([])).empty


# ── 샤프(초과수익) ──
def _ret(n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(rng.normal(0.0005, 0.01, n), index=idx)


def test_sharpe_excess_of_cash():
    ret = _ret()
    rf = pd.Series(0.0002, index=ret.index)
    ex = ret - 0.0002
    want = round(float(ex.mean() / ex.std() * math.sqrt(252)), 2)
    assert backtest.stats(ret, rf=rf)["sharpe"] == want
    assert backtest.stats(ret, rf=rf)["sharpe"] < backtest.stats(ret)["sharpe"]


def test_sharpe_drops_days_without_rf():
    """현금 금리가 없는 날(한국 2012년 이전)은 0 으로 채우지 않고 샤프 계산에서만 뺀다."""
    ret = _ret()
    rf = pd.Series(0.0002, index=ret.index[150:])
    ex = (ret - rf.reindex(ret.index)).dropna()
    want = round(float(ex.mean() / ex.std() * math.sqrt(252)), 2)
    st = backtest.stats(ret, rf=rf)
    assert st["sharpe"] == want
    assert st["cagr"] == backtest.stats(ret)["cagr"]          # 수익률·낙폭은 전 구간 그대로


def test_sharpe_falls_back_to_raw_when_rf_too_short():
    ret = _ret()
    rf = pd.Series(0.0002, index=ret.index[-10:])
    assert backtest.stats(ret, rf=rf)["sharpe"] == backtest.stats(ret)["sharpe"]


# ── 총수익 이력 잇기(splice) ──
def test_splice_rescales_old_history_at_overlap():
    old = pd.Series([100.0, 101, 102, 103], index=D[:4])
    new = pd.Series([51.5, 52.0, 53.0], index=D[3:6])         # 배당 뒤 다시 스케일된 새 구간
    out = R.splice(old, new)
    assert out.index.equals(D[:6])
    assert out[D[3]] == 51.5
    assert out[D[2]] / out[D[3]] == pytest.approx(102 / 103)  # 겹치는 날 앞의 수익률 보존


def test_splice_without_overlap_keeps_both():
    out = R.splice(pd.Series([1.0, 2.0], index=D[:2]), pd.Series([3.0], index=D[5:6]))
    assert list(out.values) == [1.0, 2.0, 3.0]


# ── VIX 빈칸 ──
def test_fill_vix_holes_only_on_spx_trading_days():
    df = pd.DataFrame({"^GSPC": [1, 1, 1, np.nan, 1, 1, 1], "^VIX": [20, np.nan, 22, np.nan, np.nan, np.nan, np.nan]},
                      index=D[:7], dtype=float)
    out = R._fill_vix_holes(df.copy(), limit=2)
    assert out["^VIX"].iloc[1] == 20          # S&P 거래일 빈칸 → 전날 값
    assert np.isnan(out["^VIX"].iloc[3])      # S&P 도 쉰 날은 그대로
    assert out["^VIX"].iloc[4] == 22          # 직전 값에서 2칸까지
    assert np.isnan(out["^VIX"].iloc[5])      # limit 넘으면 비운다


# ── 티커 → RIC ──
@pytest.mark.parametrize("t, ric", [("^KS11", ".KS11"), ("KRW=X", "KRW=KFTC"), ("JPY=X", "JPY="), ("EURUSD=X", "EUR="),
                                    ("CNY=X", "CNY="), ("^GSPC", ".SPX"), ("SPY", "SPY.P"), ("005930.KS", "005930.KS"),
                                    ("KRTSY3Y=KQ", "KRTSY3Y=KQ"), ("CLc1", "CLc1"), (".KSVKOSPI", ".KSVKOSPI")])
def test_ric_for_mapping(t, ric):
    assert R.ric_for(t) == ric


@pytest.mark.parametrize("t", ["AUDUSD=X", "BZ=F", "^ZZZ"])
def test_ric_for_rejects_unmapped_yfinance_style(t):
    """표에 없는 yfinance 식 환율·선물·지수 표기는 그대로 넘기지 않는다(JPY=X 가 다른 RIC 으로 조용히 값을 준 사고)."""
    with pytest.raises(KeyError):
        R.ric_for(t)


def test_ticker_table_has_no_commented_out_entries():
    src = (Path(R.__file__)).read_text(encoding="utf-8")
    block = src[src.index("TICKER_RIC = {"):src.index("}", src.index("TICKER_RIC = {"))]
    for line in block.splitlines():
        if "#" in line:
            assert '": "' not in line.split("#", 1)[1], f"주석 뒤에 매핑이 숨어 있다: {line.strip()}"


# ── 사내 공유 접속 차단 ──
@pytest.fixture()
def lan_client(monkeypatch):
    import config
    import web
    monkeypatch.setattr(config, "LAN_SHARE", True)
    monkeypatch.setattr(config, "LAN_ALLOW", ["10.0.0.0/24"])
    monkeypatch.setitem(web.LAN, "key", "k" * 20)
    web.ALLOWED_HOSTS.add("10.0.0.5:5101")
    return web.app.test_client()


def _get(c, path, addr="10.0.0.9", **kw):
    return c.get(path, environ_base={"REMOTE_ADDR": addr}, headers={"Host": "10.0.0.5:5101"}, **kw)


def test_lan_requires_key(lan_client):
    assert _get(lan_client, "/api/state").status_code == 403
    assert _get(lan_client, "/api/state?key=wrong").status_code == 403


def test_lan_key_redirects_and_sets_cookie(lan_client):
    r = _get(lan_client, "/?key=" + "k" * 20)
    assert r.status_code == 302 and "rc_key=" in r.headers.get("Set-Cookie", "")


def test_lan_fails_closed_without_key(lan_client, monkeypatch):
    import web
    monkeypatch.setitem(web.LAN, "key", None)
    assert _get(lan_client, "/api/state").status_code == 403
    monkeypatch.setitem(web.LAN, "key", "")
    assert _get(lan_client, "/api/state?key=").status_code == 403


def test_lan_outside_subnet_blocked(lan_client):
    assert _get(lan_client, "/api/state?key=" + "k" * 20, addr="8.8.8.8").status_code == 403


def test_lan_post_is_read_only(lan_client):
    lan_client.set_cookie("rc_key", "k" * 20, domain="10.0.0.5")
    r = lan_client.post("/api/fetch", environ_base={"REMOTE_ADDR": "10.0.0.9"},
                        headers={"Host": "10.0.0.5:5101", "Cookie": "rc_key=" + "k" * 20})
    assert r.status_code == 403
