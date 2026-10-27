# -*- coding: utf-8 -*-
"""페어트레이딩 엔진(pairs) 회귀 테스트 — 합성 데이터, Refinitiv·DART·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import json
import math
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pairs as PR  # noqa: E402

CAT = PR.load_catalog()
P = CAT["params"]


# ── 합성 데이터 ──
def _ar(n, phi, sd, rng):
    s = np.zeros(n)
    e = rng.normal(0, sd, n)
    for t in range(1, n):
        s[t] = phi * s[t - 1] + e[t]
    return s


def _ret(logp):
    """로그 가격 → 일간 수익률(%) — 첫날 NaN(pair_universe.load_prices 와 같은 모양)."""
    return np.r_[np.nan, np.expm1(np.diff(logp)) * 100]


def _market(start="2015-01-02", days=900, seed=5, pairs=None):
    """pairs: [(id 접미, kind, phi)] — phi < 1 이면 공적분(a = 0.8·b + AR(phi), 반감기 −ln2/ln phi), 1 이면 서로 독립 랜덤워크.
    b 변동(2.5%)이 스프레드 충격(0.5%)보다 커야 역방향(b~a) 회귀도 잔차가 정상이 된다. 2015~ 라 공매도 금지 구간 밖."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=days)
    pairs = pairs or [("C", "보통·우선", 0.92), ("R", "보통·우선", 1.0)]
    cols, cands, rows = {}, [], []
    for tag, kind, phi in pairs:
        a, b = f"A{tag}.KS", f"B{tag}.KS"
        x = np.cumsum(rng.normal(0, 0.025, days))
        y = 0.8 * x + _ar(days, phi, 0.005, rng) if phi < 1 else np.cumsum(rng.normal(0, 0.025, days))
        cols[a], cols[b] = _ret(y), _ret(x)
        cands.append({"id": f"{a}~{b}", "a": a, "b": b, "kind": kind, "pct": 35.0 if kind == "모자회사" else None,
                      "as_of": None, "note": ""})
        for r in (a, b):
            rows.append({"ric": r, "code": r[:3], "name": f"종목{r[:3]}", "kind": "보통", "market": "KOSPI", "idx_proxy": True})
    ret = pd.DataFrame(cols, index=idx)
    tv = pd.DataFrame(5e9, index=idx, columns=ret.columns)
    return pd.DataFrame(rows).set_index("ric"), cands, ret, tv


# ── EG(statsmodels 동등) ──
def test_eg_matches_statsmodels_coint():
    from statsmodels.tsa.stattools import coint
    rng = np.random.default_rng(7)
    for trial in range(60):
        n = 240 + trial % 13                          # 240~252 — 최대 시차 ceil(12·(n/100)^¼) 가 바뀌는 구간
        x = np.cumsum(rng.normal(0, .02, n))
        kind = trial % 3
        if kind == 0:
            y = 0.9 * x + rng.normal(0, .01, n)       # 강한 공적분 → p ≈ 0
        elif kind == 1:
            y = np.cumsum(rng.normal(0, .02, n))      # 독립 → p 큼
        else:
            y = 1.3 * x + _ar(n, 0.96, 0.01, rng)     # 중간
        t, p = PR.eg(y, x)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t2, p2, _ = coint(y, x, trend="c")
        assert t == pytest.approx(t2, abs=1e-8)
        assert p == pytest.approx(p2, abs=1e-10)


def test_eg_collinear_is_p_zero():
    x = np.cumsum(np.random.default_rng(1).normal(0, .02, 250))
    assert PR.eg(2 * x + 1, x) == (-math.inf, 0.0)


# ── BH-FDR ──
def test_bh_fdr_known_values():
    q = PR.bh_fdr([0.01, 0.03, 0.031])
    # 날것 0.03·0.045·0.031 → 뒤에서부터 누적 최소 → 0.03·0.031·0.031
    assert q == pytest.approx([0.03, 0.031, 0.031])
    q = PR.bh_fdr([0.2, 0.01, 0.04, 0.03])
    assert q == pytest.approx([0.2, 0.04, 0.04 * 4 / 3, 0.04 * 4 / 3])
    assert PR.bh_fdr([0.9, 0.95]) == pytest.approx([0.95, 0.95])   # 1 상한 전 누적 최소
    assert PR.bh_fdr([1.0, 0.8]).max() <= 1.0


def test_bh_fdr_matches_statsmodels():
    from statsmodels.stats.multitest import multipletests
    p = np.random.default_rng(3).uniform(0, 0.2, 40) ** 2
    assert PR.bh_fdr(p) == pytest.approx(multipletests(p, method="fdr_bh")[1])


# ── 반감기 ──
def test_half_life_exact_decay():
    rho = 0.9
    s = rho ** np.arange(200)                         # Δs = (ρ−1)·s_{t−1} 정확히 → φ = −0.1
    assert PR.half_life(s) == pytest.approx(-math.log(2) / math.log(rho), rel=1e-9)


def test_half_life_ar1_and_edges():
    rng = np.random.default_rng(5)
    phi = 0.93
    hl = PR.half_life(_ar(20000, phi, 1.0, rng))
    assert hl == pytest.approx(-math.log(2) / math.log(phi), rel=0.1)
    assert PR.half_life(np.arange(100.0)) == math.inf             # φ = 0 → 되돌림 없음
    assert PR.half_life((-1.0) ** np.arange(100)) is None         # φ = −2 → 정의 안 됨


# ── 월말 ──
def test_month_ends_from_index():
    idx = pd.DatetimeIndex(["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-28", "2024-03-04", "2024-03-15"])
    assert PR.month_ends(idx) == [1, 3]                            # 2/29 결측 → 2/28 이 그 달 마지막 거래일
    idx2 = pd.DatetimeIndex(["2024-03-28", "2024-03-29"])          # 3/29(금) = 3월 마지막 평일 → 마지막 행도 월말
    assert PR.month_ends(idx2) == [1]


# ── 월말 시험·채택 ──
def test_two_way_eg_adopts_cointegrated_not_random_walk():
    m, c, ret, tv = _market()
    E = PR.engine(m, c, ret, tv, CAT)
    co, rw = E["tests"]["AC.KS~BC.KS"], E["tests"]["AR.KS~BR.KS"]
    n_co = sum(bool(t and t.get("adopted")) for t in co)
    n_rw = sum(bool(t and t.get("adopted")) for t in rw)
    tested = [t for t in co if t and t.get("tested")]
    assert len(tested) >= 20
    assert n_co >= 0.5 * len(tested)
    assert n_rw == 0
    for t in tested:
        assert t["p"] == max(t["p_ab"], t["p_ba"])
        assert t["beta"] == pytest.approx(0.8, abs=0.15)


def test_adoption_rule_needs_both_directions_q_and_half_life(monkeypatch):
    m, c, ret, tv = _market(days=300, pairs=[(str(i), "보통·우선", 1.0) for i in range(5)])
    fake = iter([
        {"p_ab": 0.001, "p_ba": 0.001, "hl": 10.0},   # 채택
        {"p_ab": 0.001, "p_ba": 0.20, "hl": 10.0},    # 한 방향만 — 탈락(min(p) 를 쓰면 통과해 버림)
        {"p_ab": 0.001, "p_ba": 0.001, "hl": 4.0},    # 반감기 짧음
        {"p_ab": 0.001, "p_ba": 0.001, "hl": math.inf},
        {"p_ab": 0.04, "p_ba": 0.04, "hl": 10.0},     # 양방향 < 0.05 이지만 q = 0.04·5/5 = 0.05 … 아래 확인
    ])

    def test_pair(la, lb):
        f = next(fake)
        return {**f, "p": max(f["p_ab"], f["p_ba"]), "beta": 1.0}

    monkeypatch.setattr(PR, "test_pair", test_pair)
    idx = ret.index
    mes = [i for i in PR.month_ends(idx) if i >= P["test_window"] - 1][:1]
    lp = np.log1p(ret / 100).cumsum().to_numpy()
    lr = np.log1p(ret / 100).to_numpy()
    cols = list(ret.columns)
    pairs = {x["id"]: PR.Pair(lp[:, cols.index(x["a"])], lp[:, cols.index(x["b"])],
                              lr[:, cols.index(x["a"])], lr[:, cols.index(x["b"])]) for x in c}
    T = PR.run_tests(pairs, {x["id"]: x["kind"] for x in c}, mes, P)
    got = [T[x["id"]][0] for x in c]
    assert [g["adopted"] for g in got] == [True, False, False, False, True]
    # p = [0.001, 0.2, 0.001, 0.001, 0.04] → q(0.04) = 0.04·5/4 = 0.05 ≤ 0.05
    assert got[4]["q"] == pytest.approx(0.05)
    assert got[1]["q"] == pytest.approx(0.2)


def test_adoption_p_strict_at_boundary(monkeypatch):
    # q ≥ p 라 p_ab·p_ba < p_max 조건은 p = 0.05 경계에서만 따로 일한다: 후보 하나, p_ab 0.05 → q = 0.05 ≤ fdr_q 지만 p 는 엄격 미만이어야
    m, c, ret, tv = _market(days=300, pairs=[("0", "보통·우선", 1.0)])
    monkeypatch.setattr(PR, "test_pair", lambda la, lb: {"p_ab": 0.05, "p_ba": 0.01, "p": 0.05, "beta": 1.0, "hl": 10.0})
    rs = [t for t in PR.engine(m, c, ret, tv, CAT)["tests"]["A0.KS~B0.KS"] if t]
    assert rs and all(t["q"] == pytest.approx(0.05) and not t["adopted"] for t in rs)


def test_industry_needs_return_correlation():
    rng = np.random.default_rng(4)
    idx = pd.bdate_range("2018-01-01", periods=400)
    x = np.cumsum(rng.normal(0, 0.015, 400))
    y = 0.8 * x + _ar(400, 0.5, 0.02, rng)            # 공적분이지만 일간 수익률 상관은 낮다
    ret = pd.DataFrame({"A.KS": _ret(y), "B.KS": _ret(x)}, index=idx)
    tv = pd.DataFrame(5e9, index=idx, columns=ret.columns)
    m = pd.DataFrame([{"ric": r, "name": r, "code": r, "idx_proxy": True} for r in ret.columns]).set_index("ric")
    for kind, tested in (("동종업종", False), ("보통·우선", True)):
        c = [{"id": "A.KS~B.KS", "a": "A.KS", "b": "B.KS", "kind": kind, "pct": None}]
        rs = [t for t in PR.engine(m, c, ret, tv, CAT)["tests"]["A.KS~B.KS"] if t]
        assert rs and all(bool(t["tested"]) is tested for t in rs)
        if not tested:
            assert all(t["corr"] < P["corr_min"] for t in rs)


# ── 상태기계 ──
def _run(z, adopt=(1.0, 10.0), short_ok=None, tv_ok=None, z_of=None):
    z = list(z)
    n = len(z)
    ad = adopt if callable(adopt) else (lambda j: adopt)
    wb = (lambda j: ad(j)[0] if ad(j) else None)
    return PR.machine(n, z_of or (lambda j, b: z[j]), ad, wb, short_ok or (lambda jf, side: True),
                      tv_ok or (lambda j: True), P)


def test_entry_fills_next_day_and_normal_exit():
    trs, bu, end = _run([0, 0, 2.5, 2.4, 1.5, 0.8, 0.4, 0.3, 0.2])
    assert len(trs) == 1
    t = trs[0]
    assert (t["sig"], t["ent"], t["xsig"], t["ext"], t["reason"]) == (2, 3, 6, 7, PR.NORMAL)
    assert t["side"] == PR.SHORT_A                    # z > 0 → a 매도·b 매수
    trs, _, _ = _run([0, -2.5, -1, 0.1, 0])
    assert trs[0]["side"] == PR.LONG_A and trs[0]["ent"] == 2


def test_entry_band_is_strict_and_below_stop():
    assert _run([0, 3.5, 3.5, 3.5, 3.5])[0] == []     # |z| ≥ stop_z — 진입 안 함
    assert _run([0, 3.0, 3.0, 3.0])[0] == []
    assert _run([0, 2.0, 2.0, 2.0])[0] == []          # 엄격 부등호
    assert len(_run([0, 2.01, 1.0, 0.1, 0.0])[0]) == 1


def test_stop_then_cooldown_blocks_reentry():
    hl = 10.0
    z = [0, 2.5, 2.8, 3.2] + [2.5] * 30
    trs, _, end = _run(z, adopt=(1.0, hl))
    first, second = trs[0], trs[1]
    assert (first["xsig"], first["ext"], first["reason"]) == (3, 4, PR.STOP)
    cool_last = 4 + math.ceil(hl)                     # 손절 체결일 + ceil(반감기)
    assert second["sig"] == cool_last + 1             # 쿨다운 동안 |z|=2.5 여도 진입 없음
    # 정상 청산 뒤에는 쿨다운이 없다 — 청산 체결 다음 날 바로 신호
    trs, _, _ = _run([0, 2.5, 2.4, 0.3, 2.5, 2.5, 2.5])
    assert trs[0]["ext"] == 4 and trs[1]["sig"] == 5


def test_time_stop_min_of_two_half_lives_and_60():
    z = [0, 2.5] + [2.5] * 100
    t = _run(z, adopt=(1.0, 10.0))[0][0]
    assert t["reason"] == PR.TIME and t["xsig"] - t["ent"] == 20   # 2 × 10
    t = _run(z, adopt=(1.0, 40.0))[0][0]
    assert t["reason"] == PR.TIME and t["xsig"] - t["ent"] == 60   # min(80, 60)
    t = _run(z, adopt=(1.0, 12.3))[0][0]
    assert t["xsig"] - t["ent"] == 25                               # 보유일 ≥ 24.6


def test_signal_on_last_day_is_pending_entry():
    trs, _, end = _run([0, 0, 0, 2.5])
    assert trs == [] and end["signal"][0] == PR.SHORT_A


def test_beta_fixed_at_entry_after_retest():
    # j < 5: 시험 β = 1.0, j ≥ 5: 재검정 β = 2.0. z 는 β=1 이면 2.5, β=2 이면 0 — 시험 β 로 z 를 보면 곧바로 정상 청산된다.
    adopt = (lambda j: (1.0, 10.0) if j < 5 else (2.0, 10.0))
    trs, bu, _ = _run([None] * 40, adopt=adopt, z_of=lambda j, b: 2.5 if b == 1.0 else 0.0)
    t = trs[0]
    assert t["beta"] == 1.0 and t["sig"] == 0 and t["ent"] == 1
    assert t["reason"] == PR.TIME and t["xsig"] == 21               # z 가 계속 2.5 → 시간 손절까지 보유
    assert all(b == 1.0 for b in bu[t["ent"]:t["ext"] + 1])
    assert bu[t["ext"] + 1] == 2.0                                  # 청산 뒤엔 대기 β(최근 채택)


def test_retest_failure_does_not_exit():
    adopt = (lambda j: (1.0, 10.0) if j < 3 else None)
    trs, _, _ = _run([0, 2.5] + [2.5] * 40, adopt=adopt)
    assert trs[0]["reason"] == PR.TIME


def _short_ok(dates, ia, ib):
    def f(jf, side):
        d = dates[jf] if jf < len(dates) else dates[-1] + pd.offsets.BDay(1)    # 데이터 끝 신호 → 다음 평일
        return PR.short_allowed(PR.regime(CAT, d), ib if side == PR.LONG_A else ia)
    return f


def test_short_regime_blocks_entry():
    banned = pd.bdate_range("2024-03-04", periods=10)
    assert _run([0, 2.5, 2.5, 0.1, 0, 0, 0, 0, 0, 0], short_ok=_short_ok(banned, True, True))[0] == []
    partial = pd.bdate_range("2022-03-02", periods=10)
    z_up = [0, 2.5, 2.5, 0.1, 0, 0, 0, 0, 0, 0]       # z > 0 → 숏 a
    assert _run(z_up, short_ok=_short_ok(partial, False, True))[0] == []      # a 가 지수 밖 → 불가
    assert len(_run(z_up, short_ok=_short_ok(partial, True, False))[0]) == 1  # a 가 idx_proxy → 허용
    z_dn = [-x for x in z_up]                          # z < 0 → 숏 b
    assert len(_run(z_dn, short_ok=_short_ok(partial, False, True))[0]) == 1
    assert _run(z_dn, short_ok=_short_ok(partial, True, False))[0] == []
    assert len(_run(z_up, short_ok=_short_ok(pd.bdate_range("2019-03-04", periods=10), False, False))[0]) == 1


def test_short_regime_uses_fill_date():
    # 2020-03-13(금, open) 신호 → 2020-03-16(월, banned) 체결 → 진입 불가
    d = pd.DatetimeIndex(["2020-03-12", "2020-03-13", "2020-03-16", "2020-03-17", "2020-03-18"])
    assert _run([0, 2.5, 2.5, 0.1, 0], short_ok=_short_ok(d, True, True))[0] == []
    # 2021-04-30(banned) 신호 → 2021-05-03(partial, 숏 레그 idx_proxy) 체결 → 허용
    d = pd.DatetimeIndex(["2021-04-29", "2021-04-30", "2021-05-03", "2021-05-04", "2021-05-06"])
    trs = _run([0, 2.5, 2.5, 0.1, 0], short_ok=_short_ok(d, True, True))[0]
    assert len(trs) == 1 and d[trs[0]["ent"]] == pd.Timestamp("2021-05-03")


def test_ban_start_does_not_force_exit():
    d = pd.bdate_range("2023-10-23", periods=80)       # partial → 2023-11-06 부터 banned
    trs, _, _ = _run([0, 2.5] + [2.5] * 78, adopt=(1.0, 40.0), short_ok=_short_ok(d, True, True))
    t = trs[0]
    assert d[t["ent"]] < pd.Timestamp("2023-11-06") < d[t["ext"]]
    assert t["reason"] == PR.TIME and t["xsig"] - t["ent"] == 60


def test_liquidity_filter_blocks_entry():
    assert _run([0, 2.5, 2.5, 0.1, 0], tv_ok=lambda j: False)[0] == []


# ── 비용·수익 ──
def _trade(side, beta, ent, ext):
    return {"side": side, "beta": beta, "hl": 10.0, "ent": ent, "ext": ext}


def test_sell_tax_by_fill_date():
    assert PR.tax_rate(CAT, "2019-05-31") == 0.30
    assert PR.tax_rate(CAT, "2019-06-03") == 0.25
    assert PR.tax_rate(CAT, "2020-12-31") == 0.25
    assert PR.tax_rate(CAT, "2021-01-04") == 0.23
    assert PR.tax_rate(CAT, "2023-06-01") == 0.20
    assert PR.tax_rate(CAT, "2024-06-03") == 0.18
    assert PR.tax_rate(CAT, "2025-06-02") == 0.15
    assert PR.tax_rate(CAT, "2026-10-27") == 0.20


def test_sell_tax_short_leg_at_entry_long_leg_at_exit():
    dates = pd.DatetimeIndex(["2019-05-31", "2019-06-03", "2021-01-04", "2021-01-05"])
    zero = np.zeros(4)
    fee, bf = P["commission_slip_per_side"], P["borrow_fee_annual"] / P["days_per_year"]
    # β=3 → w_a 0.25, w_b 0.75. 숏 a: 진입 거래세 0.30×0.25(숏 a), 청산 0.23×0.75(롱 b 매도)
    r = PR.pnl(_trade(PR.SHORT_A, 3.0, 0, 2), zero, zero, dates, CAT)
    assert r["gross"] == 0.0
    assert r["net"] == pytest.approx(-(2 * fee + 0.30 * 0.25 + 0.23 * 0.75 + 0.25 * bf * 2))
    # 롱 a(숏 b): 진입 0.30×0.75(숏 b), 청산 0.23×0.25(롱 a), 대차료는 숏 b 비중
    r = PR.pnl(_trade(PR.LONG_A, 3.0, 0, 2), zero, zero, dates, CAT)
    assert r["net"] == pytest.approx(-(2 * fee + 0.30 * 0.75 + 0.23 * 0.25 + 0.75 * bf * 2))
    # 진입 체결일이 하루 늦으면(2019-06-03) 0.25
    r = PR.pnl(_trade(PR.SHORT_A, 3.0, 1, 3), zero, zero, dates, CAT)
    assert r["net"] == pytest.approx(-(2 * fee + 0.25 * 0.25 + 0.23 * 0.75 + 0.25 * bf * 2))


def test_borrow_fee_accrues_per_holding_day():
    dates = pd.bdate_range("2024-01-02", periods=60)
    zero = np.zeros(60)
    short = PR.pnl(_trade(PR.SHORT_A, 1.0, 0, 5), zero, zero, dates, CAT)
    long_ = PR.pnl(_trade(PR.SHORT_A, 1.0, 0, 45), zero, zero, dates, CAT)
    assert long_["days"] == 45 and short["days"] == 5
    assert short["net"] - long_["net"] == pytest.approx(0.5 * 3.0 / 252 * 40)


def test_gross_net_and_cost_sensitivity():
    dates = pd.bdate_range("2024-01-02", periods=6)
    ra = np.array([0, 1.0, -2.0, 0.5, 0.0, 3.0])       # 일간 %
    rb = np.array([0, 0.5, 1.0, -1.0, 2.0, 0.0])
    ca, cb = np.cumsum(ra), np.cumsum(rb)
    t = _trade(PR.LONG_A, 1.0, 1, 4)                   # 수익은 체결 다음 날(2)부터 청산 체결일(4)까지
    r = PR.pnl(t, ca, cb, dates, CAT)
    gross = 0.5 * (-2.0 + 0.5 + 0.0) - 0.5 * (1.0 - 1.0 + 2.0)
    assert r["gross"] == pytest.approx(gross)
    tax = 0.18 * 0.5 * 2
    borrow = 0.5 * 3.0 / 252 * 3
    assert r["net"] == pytest.approx(gross - 2 * 0.15 - tax - borrow)
    assert r["net_sens"] == pytest.approx(gross - 2 * 0.3 - tax - borrow)
    t2 = _trade(PR.SHORT_A, 1.0, 1, 4)
    assert PR.pnl(t2, ca, cb, dates, CAT)["gross"] == pytest.approx(-gross)


def test_open_trade_is_unrealized_and_excluded_from_stats():
    s = PR.trade_stats([{"exit": None, "net": 5.0, "gross": 5.0, "days": 3, "reason": None},
                        {"exit": pd.Timestamp("2024-01-05"), "net": -1.0, "gross": 0.0, "days": 2, "reason": PR.STOP}])
    assert s["n"] == 1 and s["win"] == 0.0 and s["mean"] == -1.0 and s["reasons"][PR.STOP] == 1


# ── 미래 값 없음 ──
def test_month_end_test_uses_only_past_data():
    m, c, ret, tv = _market()
    E1 = PR.engine(m, c, ret, tv, CAT)
    k = len(E1["mes"]) // 2
    cut = E1["idx"][E1["mes"][k]]
    noisy = ret.copy()
    after = noisy.index > cut
    noisy.loc[after] = noisy.loc[after] + np.random.default_rng(9).normal(0, 3, (after.sum(), noisy.shape[1]))
    E2 = PR.engine(m, c, noisy, tv, CAT)
    assert E2["mes"] == E1["mes"]
    for pid in E1["tests"]:
        assert E1["tests"][pid][:k + 1] == E2["tests"][pid][:k + 1]
        assert E1["tests"][pid][k + 1:] != E2["tests"][pid][k + 1:]   # 교란이 실제로 결과를 바꿨다(테스트가 헛돌지 않게)
    done1 = [(t["entry"], t["exit"], t["gross"]) for r in E1["runs"].values() for t in r["trades"]
             if t["exit"] is not None and t["exit"] <= cut]
    done2 = [(t["entry"], t["exit"], t["gross"]) for r in E2["runs"].values() for t in r["trades"]
             if t["exit"] is not None and t["exit"] <= cut]
    assert done1 and done1 == done2


def test_engine_trade_beta_is_test_beta_at_signal():
    m, c, ret, tv = _market()
    E = PR.engine(m, c, ret, tv, CAT)
    run, rs = E["runs"]["AC.KS~BC.KS"], E["tests"]["AC.KS~BC.KS"]
    assert run["trades"]
    for t in run["trades"]:
        k = run["kk"][t["sig"]]
        assert rs[k]["adopted"] and t["beta"] == rs[k]["beta"] and t["hl"] == rs[k]["hl"]
        assert t["entry"] > t["entry_signal"]


def test_liquidity_threshold_in_engine():
    m, c, ret, tv = _market()
    assert PR.engine(m, c, ret, tv, CAT)["runs"]["AC.KS~BC.KS"]["trades"]
    small = tv * 0 + 9e8                                 # 10억 미만
    assert PR.engine(m, c, ret, small, CAT)["runs"]["AC.KS~BC.KS"]["trades"] == []


# ── 출력 ──
def test_compute_outputs_json_safe_and_schema():
    m, c, ret, tv = _market(pairs=[("C", "보통·우선", 0.92), ("R", "동종업종", 1.0),
                                                      ("D", "모자회사", 0.92), ("Z", "보통·우선", 1.0)])
    ret.loc[ret.index[:700], "AZ.KS"] = np.nan        # 이력 부족 페어
    ret.loc[ret.index[300:320], "BD.KS"] = np.nan     # 거래정지 구간
    c.append({"id": "AX.KS~BX.KS", "a": "AX.KS", "b": "BX.KS", "kind": "보통·우선", "pct": None, "as_of": None, "note": ""})
    doc, charts = PR.compute(m, c, ret, tv, CAT)
    json.dumps(doc, allow_nan=False, ensure_ascii=False)
    json.dumps(charts, allow_nan=False, ensure_ascii=False)
    meta = doc["meta"]
    assert meta["n_candidates"] == {"보통·우선": 3, "모자회사": 1, "동종업종": 1}
    first = next(i for i in PR.month_ends(ret.index) if i >= P["test_window"] - 1)
    assert meta["backtest_start"] == str(ret.index[first].date())
    assert {x["id"] for x in doc["missing"]} == {"AX.KS~BX.KS", "AZ.KS~BZ.KS"}
    assert set(doc["backtest"]) == {"overall", "by_kind", "by_year", "cost_sens", "by_pct_bin", "reference"}
    assert [b["bin"] for b in doc["backtest"]["by_pct_bin"]] == ["7–20", "20–30", "30–50", "50+"]
    assert doc["backtest"]["by_pct_bin"][2]["n_tests"] > 0           # 모자회사 35% → 30–50
    assert doc["backtest"]["overall"]["n"] > 0
    ids = {r["id"] for r in doc["pairs"]}
    assert "AC.KS~BC.KS" in ids and set(charts) == {r["id"] for r in doc["pairs"] if r["state"] != PR.ST_WATCH}
    for r in doc["pairs"]:
        assert r["state"] in PR.STATE_ORDER
        assert r["short_badge"] in ("미확인", "불가", "제한")
        assert isinstance(r["warnings"], list)
    ch = charts["AC.KS~BC.KS"]
    assert len(ch["dates"]) == len(ch["z"]) == len(ch["s"])
    assert ch["dates"][0] >= str((ret.index[-1] - pd.DateOffset(years=3)).date())
    assert all(set(t) >= {"entry_signal", "entry", "exit", "side", "reason", "gross", "net", "days"} for t in ch["trades"])


def test_today_cuts_data():
    m, c, ret, tv = _market()
    cut = ret.index[700]
    doc, _ = PR.compute(m, c, ret, tv, CAT, today=cut)
    assert doc["meta"]["as_of"] == str(cut.date())


def test_z_matches_direct_window_calc():
    m, c, ret, tv = _market()
    ret.loc[ret.index[600:610], "AC.KS"] = np.nan     # 창 안 결측 — 그날만 빠진다
    E = PR.engine(m, c, ret, tv, CAT)
    run = E["runs"]["AC.KS~BC.KS"]
    lp = np.log1p(ret.loc[E["idx"]] / 100).cumsum()   # 엔진 인덱스(전부 NaN 인 첫 행은 빠진다)
    for i in (620, 700, len(E["idx"]) - 1):           # 데이터 인덱스 위치
        j = int(np.searchsorted(run["J"], i))
        assert run["J"][j] == i
        w = lp.iloc[i - P["z_window"] + 1:i + 1].dropna()
        s = w["AC.KS"] - 0.8 * w["BC.KS"]
        assert run["z_of"](j, 0.8) == pytest.approx((s.iloc[-1] - s.mean()) / s.std(ddof=1), rel=1e-7)


def _row_at(m, c, ret, tv, today, pid="AC.KS~BC.KS"):
    doc, charts = PR.compute(m, c, ret, tv, CAT, today=today)
    return next((r for r in doc["pairs"] if r["id"] == pid), None), doc


def test_current_row_states_by_cutoff():
    m, c, ret, tv = _market()
    E = PR.engine(m, c, ret, tv, CAT)
    run, rs = E["runs"]["AC.KS~BC.KS"], E["tests"]["AC.KS~BC.KS"]
    d = run["dates"]
    t = next(t for t in run["trades"] if t["ext"] - t["ent"] >= 4)
    # 보유 중(진입 체결 3 공동 거래일 뒤) — 같은 진입, 시험 채택 여부에 따라 '보유 중'·'미채택 — 보유 중'
    cut = d[t["ent"] + 3]
    row, doc = _row_at(m, c, ret, tv, cut)
    k = max(i for i, p in enumerate(E["mes"]) if E["idx"][p] <= cut)
    assert row["state"] == (PR.ST_HOLD if rs[k]["adopted"] else PR.ST_HOLD_OUT)
    assert row["side"] == PR.SIDE[t["side"]] and row["entry"] == str(t["entry"].date()) and row["hold_days"] == 3
    assert row["beta"] == round(t["beta"], 3) and row["pnl_net"] is not None
    assert doc["meta"]["n_open"] == 1
    # 진입 신호(데이터 끝 신호 — 아직 체결 전)
    row, _ = _row_at(m, c, ret, tv, d[t["sig"]])
    assert row["state"] == PR.ST_SIGNAL and row["side"] == PR.SIDE[t["side"]] and row["hold_days"] is None
    # 쿨다운(손절 체결 다음 공동 거래일)
    st = next(t for t in run["trades"] if t["reason"] == PR.STOP)
    row, _ = _row_at(m, c, ret, tv, d[st["ext"] + 1])
    assert row["state"] == PR.ST_COOL and row["side"] is None


def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(PR, "OUT", tmp_path / "pairs.json")
    monkeypatch.setattr(PR, "CHARTS", tmp_path / "pairs_charts.json")
    m, c, ret, tv = _market()
    doc, charts = PR.compute(m, c, ret, tv, CAT)
    PR._save(PR.CHARTS, charts)
    PR._save(PR.OUT, doc)
    assert not list(tmp_path.glob("*.tmp"))
    assert PR.load()["meta"]["as_of"] == doc["meta"]["as_of"]
    pid = next(iter(charts))
    assert PR.load_chart(pid) == charts[pid] and PR.load_chart("없음") is None
    assert 0 <= PR.age_hours() < 1


# ── 관찰 목록·판정 한 줄(2026-10-28 사용자 결정: FDR 기준 유지 + 관찰) ──
def _cat(**kw):
    import copy
    c = copy.deepcopy(CAT)
    c["params"].update(kw)
    return c


WATCH_MKT = dict(seed=1, pairs=[("C", "보통·우선", 0.9), ("R", "보통·우선", 1.0)])   # 마지막 월말 p≈0.01·반감기 6.2


def test_watch_rows_when_fdr_fails_but_p_and_hl_pass():
    m, c, ret, tv = _market(**WATCH_MKT)
    strict = _cat(fdr_q=1e-300)                          # 아무도 FDR 을 못 넘게
    doc, charts = PR.compute(m, c, ret, tv, strict)
    w = {r["id"]: r for r in doc["pairs"]}
    r = w["AC.KS~BC.KS"]                                   # 공적분 페어 → 관찰
    assert r["state"] == PR.ST_WATCH and r["side"] is None and r["entry"] is None and r["pnl_net"] is None
    assert r["warnings"][0].startswith("FDR q") and "신호로 쓰지 않음" in r["warnings"][0]
    assert r["z"] is not None and r["p_ab"] < P["p_max"] and r["p_ba"] < P["p_max"]
    assert "AC.KS~BC.KS" not in charts and doc["backtest"]["overall"]["n"] == 0
    assert "AR.KS~BR.KS" not in w                          # 랜덤워크 페어는 관찰에도 없음
    # 관찰 z 는 시험 β·같은 창 정의 — 직접 계산과 같다
    idx = ret.index[-P["z_window"]:]
    lp = np.log1p(ret / 100).cumsum().loc[idx]
    sp = lp["AC.KS"] - r["beta_test"] * lp["BC.KS"]
    assert abs((sp.iloc[-1] - sp.mean()) / sp.std() - r["z"]) < 0.02
    # FDR 을 넘으면(채택) 같은 페어가 관찰이 아니다 — 대기·신호·보유 쪽
    loose = {x["id"]: x["state"] for x in PR.compute(m, c, ret, tv, _cat(fdr_q=1.0))[0]["pairs"]}
    assert loose["AC.KS~BC.KS"] in (PR.ST_WAIT, PR.ST_SIGNAL, PR.ST_HOLD, PR.ST_COOL)


def test_watch_requires_half_life_in_range():
    m, c, ret, tv = _market(**WATCH_MKT)
    doc, _ = PR.compute(m, c, ret, tv, _cat(fdr_q=1e-300, hl_max=1))
    assert not any(r["state"] == PR.ST_WATCH for r in doc["pairs"])


def test_verdict_text_and_signal_flag():
    v = PR.verdict({"n": 1, "mean": 6.6, "win": 100.0}, {"n": 322, "win": 42.9, "mean": -0.7}, P)
    assert v["is_signal"] is False and "표본이 부족" in v["text"] and "322건" in v["text"] and "−" not in v["text"]
    assert PR.verdict({"n": 30, "mean": -0.2, "win": 40.0}, None, P)["is_signal"] is False
    ok = PR.verdict({"n": 30, "mean": 0.8, "win": 60.0}, None, P)
    assert ok["is_signal"] is True and "0.8%" in ok["text"]


def test_reference_backtest_without_fdr():
    m, c, ret, tv = _market()
    doc, _ = PR.compute(m, c, ret, tv, _cat(fdr_q=1e-300, reference_fdr_q=1.0))
    ref = doc["backtest"]["reference"]
    assert ref["fdr_q"] == 1.0 and ref["n"] > 0 and doc["backtest"]["overall"]["n"] == 0
    assert doc["meta"]["verdict"]["is_signal"] is False and f"{ref['n']}건" in doc["meta"]["verdict"]["text"]
    assert PR.compute(m, c, ret, tv, _cat(reference_fdr_q=None))[0]["backtest"]["reference"] is None


def test_negative_beta_never_adopted_or_watched():
    # a = −0.8·b + 정상 잔차 → 공적분이지만 β < 0 — 숏 레그 없는 같은 방향 헤지라 채택·관찰 안 함
    m, c, ret, tv = _market(**WATCH_MKT)
    lp = np.log1p(ret / 100).cumsum()
    neg = -0.8 * lp["BC.KS"] + (lp["AC.KS"] - 0.8 * lp["BC.KS"])
    ret["AC.KS"] = (np.expm1(neg.diff()) * 100).fillna(0.0)
    E = PR.engine(m, c, ret, tv, _cat(fdr_q=1.0))
    tested = [t for t in E["tests"]["AC.KS~BC.KS"] if t and t.get("tested")]
    assert any(t["p"] < P["p_max"] and t["beta"] < 0 for t in tested)
    assert not any(t["adopted"] for t in tested) and "AC.KS~BC.KS" not in E["runs"]
    doc, _ = PR.compute(m, c, ret, tv, _cat(fdr_q=1e-300))
    assert "AC.KS~BC.KS" not in {r["id"] for r in doc["pairs"]}


# ── 2026-10-28 리뷰 반영 ──
def test_exit_uses_signed_z_overshoot_is_normal_not_stop():
    # 숏 a(z>2 진입) — 평균을 지나쳐 −3.5 로 튀면 정상 청산(이익), 손절이 아니다
    trs, _, _ = _run([0, 2.5, 2.4, -3.5, -3.5, 0, 0])
    assert trs[0]["reason"] == PR.NORMAL and trs[0]["xsig"] == 3
    # 롱 a(z<−2 진입) — +4 로 튀어도 정상
    trs, _, _ = _run([0, -2.5, -2.4, 4.0, 0, 0])
    assert trs[0]["reason"] == PR.NORMAL
    # 진입 쪽으로 더 벌어지면 손절(부호 그대로)
    assert _run([0, -2.5, -2.4, -3.2, 0, 0])[0][0]["reason"] == PR.STOP
    assert _run([0, 2.5, 2.4, 3.2, 0, 0])[0][0]["reason"] == PR.STOP
    # 진입 쪽 0.5 아래로만 오면 정상 — +0.4 는 숏 a 청산, 롱 a 는 −0.4 에서 청산
    assert _run([0, 2.5, 2.4, 0.4, 0, 0])[0][0]["xsig"] == 3
    assert _run([0, -2.5, -2.4, -0.4, 0, 0])[0][0]["xsig"] == 3


def test_month_end_skips_krx_dec31_holiday():
    idx = pd.DatetimeIndex(pd.bdate_range("2025-12-01", "2025-12-30"))      # 12-31 은 KRX 휴장
    assert PR.month_ends(idx)[-1] == len(idx) - 1                           # 12-30 이 12월 월말 시험
    assert PR.last_session("2025-12-15") == pd.Timestamp("2025-12-30")
    assert PR.last_session("2026-10-02") == pd.Timestamp("2026-10-30")
    idx2 = pd.DatetimeIndex(pd.bdate_range("2026-10-01", "2026-10-29"))
    assert len(idx2) - 1 not in PR.month_ends(idx2)                         # 10-29 는 아직 월말 아님


def test_row_with_past_trades_shows_as_watch_when_only_fdr_fails():
    # 예전에 채택돼 거래가 있었던 페어라도 지금 시험이 FDR 만 탈락이면 '미채택'이 아니라 '관찰'(시험 β 의 z)
    m, c, ret, tv = _market(**WATCH_MKT)
    cat = _cat(fdr_q=1.0, recent_months=1200)
    E = PR.engine(m, c, ret, tv, cat)
    pid = "AC.KS~BC.KS"
    assert E["runs"][pid]["trades"]                        # 과거 거래 있음
    lt = E["tests"][pid][-1]
    lt.update(adopted=False, q=0.9)                        # 마지막 시험만 FDR 탈락으로
    run = E["runs"][pid]
    if run["trades"][-1]["ext"] is None:                   # 열린 거래가 있으면 '미채택 — 보유 중'이 우선 — 닫힌 상태로 본다
        run["trades"].pop()
    run["end"].update(signal=None, cool=-1)
    as_of = E["idx"][-1]
    cand = next(x for x in c if x["id"] == pid)
    assert PR._row(cand, E, m, cat, as_of, None) is None
    w = PR._watch_row(cand, E, m, cat, ret, tv, as_of, None)
    assert w["state"] == PR.ST_WATCH and w["beta"] == round(lt["beta"], 3)
    lt.update(p_ab=0.2)                                    # p 도 탈락이면 관찰 아님 → 최근 거래 있는 '미채택'
    assert PR._row(cand, E, m, cat, as_of, None)["state"] == PR.ST_OUT
