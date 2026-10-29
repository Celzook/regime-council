# -*- coding: utf-8 -*-
"""변동성 급등 이벤트 스터디(spikes) 회귀 테스트 — 합성 데이터, Refinitiv·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import spikes as S  # noqa: E402


def _ser(vals, start="2010-01-04"):
    return pd.Series(np.asarray(vals, float), index=pd.bdate_range(start, periods=len(vals)))


def _row(h, n, k):
    return {"h": h, "days": S.H[h], "n": n, "wins": k}


# ── 이벤트 엔진 ──
def test_long_episode_is_one_event():
    # 30 위에서 오래 머물고 중간에 26~29 로 내려와도(25 미만이 아니면) 재무장 안 됨 → 1건
    x = _ser([15] * 30 + [35] * 40 + [27] * 10 + [45] * 40 + [26] * 5 + [33] * 30 + [15] * 20)
    ev = S.events(x, S._ge(30), S._lt(25))
    assert ev == [x.index[30]]
    # 매일 세면 110건(과대평가 비교용)
    assert len(S.every_day(x, S._ge(30))) == 110


def test_two_separated_spikes_are_two_events():
    x = _ser([15] * 30 + [35] * 5 + [15] * 30 + [35] * 5 + [15] * 10)
    assert S.events(x, S._ge(30), S._lt(25)) == [x.index[30], x.index[65]]


def test_min_gap_20_days_merges_quick_rearm():
    # 25 미만으로 잠깐 내려가 재무장돼도 직전 이벤트에서 20거래일이 안 됐으면 세지 않는다
    x = _ser([15] * 10 + [35] * 3 + [20] * 3 + [35] * 5 + [15] * 40)
    assert S.events(x, S._ge(30), S._lt(25)) == [x.index[10]]
    # 같은 모양에 최소 간격을 끄면 2건(히스테리시스만)
    assert len(S.events(x, S._ge(30), S._lt(25), min_gap=0)) == 2
    # 20거래일 뒤의 재발은 따로 센다
    y = _ser([15] * 10 + [35] * 3 + [20] * 17 + [35] * 5 + [15] * 10)
    assert S.events(y, S._ge(30), S._lt(25)) == [y.index[10], y.index[30]]


def test_start_restricts_scan():
    x = _ser([15] * 10 + [35] * 3 + [15] * 30 + [35] * 3 + [15] * 10)
    assert S.events(x, S._ge(30), S._lt(25), start=x.index[20]) == [x.index[43]]


def test_start_mid_episode_makes_no_artificial_event():
    # 시작일에 이미 진행 중인 급등 구간(이벤트는 시작일 전 10번째 날) → 시작일에 이벤트를 만들지 않는다
    x = _ser([15] * 10 + [35] * 30 + [15] * 20)
    assert S.events(x, S._ge(30), S._lt(25)) == [x.index[10]]
    assert S.events(x, S._ge(30), S._lt(25), start=x.index[20]) == []
    # 시작일 전 이벤트의 최소 간격(20거래일)도 이어진다: 재무장돼도 간격이 안 찼으면 세지 않음
    y = _ser([15] * 10 + [35] * 3 + [20] * 3 + [35] * 5 + [15] * 40)
    assert S.events(y, S._ge(30), S._lt(25), start=y.index[15]) == []


def test_rearm_boundary_is_strictly_below():
    # 정확히 25 는 재무장 아님(< 25) → 두 번째 급등은 같은 구간
    x = _ser([15] * 10 + [35] * 3 + [25] * 30 + [35] * 3 + [15] * 10)
    assert S.events(x, S._ge(30), S._lt(25)) == [x.index[10]]
    y = _ser([15] * 10 + [35] * 3 + [24.99] * 30 + [35] * 3 + [15] * 10)
    assert S.events(y, S._ge(30), S._lt(25)) == [y.index[10], y.index[43]]


def test_vkospi_predicate_needs_level_and_1y_percentile():
    # 1년 넘게 60 → 50 으로 내려오다 45 → 수준은 내내 ≥40 이지만 1년 백분위가 낮아 이벤트 아님
    rng = np.random.default_rng(1)
    high = _ser(list(np.linspace(60, 50, 300)) + [45] * 30)
    assert S.events(high, S._vkospi, S._lt(30)) == []
    # 20 근처에서 45 로 급등 → 백분위 ~100 → 이벤트
    calm = _ser(list(20 + rng.normal(0, 1, 300)) + [45] * 30 + [20] * 30)
    assert S.events(calm, S._vkospi, S._lt(30)) == [calm.index[300]]
    # 252일이 차기 전에는 백분위가 없어 급등해도 세지 않는다(미래 값 사용 없음)
    early = _ser([20] * 100 + [45] * 5 + [20] * 200)
    assert S.events(early, S._vkospi, S._lt(30)) == []


def test_pct_1y_mid_rank_matches_level_stats_definition():
    s = _ser(list(range(1, 253)))
    p = S.pct_1y(s)
    assert p.iloc[:251].isna().all()
    assert p.iloc[-1] == pytest.approx((251 / 252 + 0.5 / 252) * 100)


# ── 통계 ──
def test_wilson_lower_bounds():
    # 단측 90%(z=1.2816)
    assert S.WILSON_Z == pytest.approx(1.2816, abs=1e-4)
    assert S.wilson(13, 13) == pytest.approx(0.8878, abs=1e-4)
    assert S.wilson(9, 9) == pytest.approx(0.8457, abs=1e-4)
    assert S.wilson(27, 30) == pytest.approx(0.8078, abs=1e-4)
    assert S.wilson(8, 8) == pytest.approx(0.8297, abs=1e-4)
    assert S.wilson(0, 0) != S.wilson(0, 0)   # NaN


def test_reach_90_smallest_horizon_held_for_all_longer():
    tab = [_row("1M", 10, 6), _row("3M", 10, 8), _row("6M", 10, 10), _row("12M", 10, 10), _row("24M", 9, 9), _row("36M", 9, 9)]
    assert S.reach_90(tab) == "6M"


def test_reach_90_later_dip_pushes_out():
    # 6M 에 넘었다가 24M 에 80% 로 내려가면 6M 이 아니라 36M
    tab = [_row("1M", 10, 6), _row("3M", 10, 8), _row("6M", 10, 10), _row("12M", 10, 10), _row("24M", 10, 8), _row("36M", 10, 10)]
    assert S.reach_90(tab) == "36M"


def test_reach_90_needs_n_8_and_lb_75():
    # 7/7 = 100% 라도 n < 8 이면 도달 안 함
    tab = [_row(h, 7, 7) for h in S.H]
    assert S.reach_90(tab) is None
    # 긴 보유기간 하나가 n < 8 이면(데이터 부족) 전체가 도달 안 함
    tab = [_row(h, 10, 10) for h in S.H][:-1] + [_row("36M", 7, 7)]
    assert S.reach_90(tab) is None
    # 9/10 = 90% 지만 Wilson 하한 72% < 75% → 도달 안 함, 20/20 은 통과
    assert S.reach_90([_row(h, 10, 9) for h in S.H]) is None
    assert S.reach_90([_row(h, 20, 20) for h in S.H]) == "1M"
    assert S.reach_90([_row(h, 0, 0) for h in S.H]) is None


def test_reach_90_n_8_gate_binds():
    # z=1.2816 에서 8/8 은 하한 83% → 도달, 7/7 은 하한 81%(≥75%)인데도 n < 8 때문에 도달 안 함
    assert S.reach_90([_row(h, 8, 8) for h in S.H]) == "1M"
    assert S.wilson(7, 7) >= S.REACH["lb"]
    assert S.reach_90([_row(h, 7, 7) for h in S.H]) is None


def test_reach_90_exact_90_percent_counts():
    # 27/30 = 정확히 90%(하한 81%) → '≥ 90%' 이므로 도달
    assert S.reach_90([_row(h, 30, 27) for h in S.H]) == "1M"


# ── 체결·비용 ──
def test_entry_is_next_trading_day_close_no_lookahead():
    idx = pd.bdate_range("2020-01-01", periods=40)
    px = pd.Series(100.0, index=idx)
    px.iloc[11:] = 200.0                     # 신호일(10번째) '다음날'부터 200
    px.iloc[11 + 21:] = 220.0
    rows = S.fwd(px, [idx[10]], cost=0.0, horizons={"1M": 21})
    assert rows[0]["entry"] == idx[11]
    assert rows[0]["cost_basis"] == 200.0
    assert rows[0]["r"]["1M"] == pytest.approx(0.10)
    # 신호일이 대상 휴장일(주말)이어도 다음 거래일
    sat = idx[10] + pd.Timedelta(days=(5 - idx[10].day_of_week) % 7 or 7)
    assert S.fwd(px, [sat], cost=0.0, horizons={"1M": 21})[0]["entry"] > sat
    # 체결일이 아직 없는 최근 신호는 표에 안 들어간다
    last = S.fwd(px, [idx[-1]], horizons={"1M": 21})[0]
    assert last["entry"] is None and S.table([last], {"1M": 21})[0]["n"] == 0


def test_entry_delay_over_14_calendar_days_is_skipped():
    # 대상 이력에 공백: 신호일 다음 거래일이 15 달력일 뒤 → 체결 안 함(사유), 정확히 14일 뒤(추석 연휴급)는 체결
    a = pd.bdate_range("2020-01-06", periods=3)                 # 월~수, 신호일 = 수 01-08
    for gap, ok in [(15, False), (14, True)]:
        idx = a.append(pd.bdate_range(a[-1] + pd.Timedelta(days=gap), periods=30))
        px = pd.Series(100.0, index=idx)
        e = S.fwd(px, [a[-1]], cost=0.0, horizons={"1M": 21})[0]
        if ok:
            assert e["entry"] == a[-1] + pd.Timedelta(days=14) and e["r"]["1M"] == 0.0 and "reason" not in e
        else:
            assert e["entry"] is None and e["r"]["1M"] is None and "15일" in e["reason"]
            assert S.table([e], {"1M": 21})[0]["n"] == 0


def test_move_no_effect_propagates_to_sub_blocks(monkeypatch):
    """MOVE 본 표가 '단독 효과 확인 안 됨'이면 최장 기간·원화 표가 우연히 좋아도 신호로 내지 않는다."""
    calls = {"n": 0}

    def fake(tab, reach):
        calls["n"] += 1
        return (S.NO_EFFECT, False) if calls["n"] == 1 else ("효과 있음(가짜)", True)
    monkeypatch.setattr(S, "move_verdict", fake)
    out = S.compute(_synthetic())
    st = next(x for x in out["studies"] if x["trigger"].startswith("move"))
    assert (st["verdict"], st["is_signal"]) == (S.NO_EFFECT, False)
    assert (st["longest"]["verdict"], st["longest"]["is_signal"]) == (S.NO_EFFECT, False)
    if "krw" in st and not st["krw"].get("skipped"):
        assert (st["krw"]["verdict"], st["krw"]["is_signal"]) == (S.NO_EFFECT, False)


def test_cost_applied_round_trip():
    px = pd.Series(100.0, index=pd.bdate_range("2020-01-01", periods=300))
    r = S.fwd(px, [px.index[0]], cost=0.001)[0]["r"]
    assert r["1M"] == pytest.approx(-0.001)
    tab = S.table(S.fwd(px, [px.index[0]], cost=0.003))
    assert tab[0]["wins"] == 0 and tab[0]["worst"] == pytest.approx(-0.3)


def test_tranches_harmonic_mean_and_valid_horizons():
    idx = pd.bdate_range("2020-01-01", periods=400)
    v = np.full(400, 100.0)
    v[1 + 21:] = 50.0
    v[1 + 42:] = 100.0
    px = pd.Series(v, index=idx)
    e = S.fwd(px, [idx[0]], cost=0.0, tranches=(0, 21, 42))[0]
    assert e["cost_basis"] == pytest.approx(3 / (1 / 100 + 1 / 50 + 1 / 100))   # 75
    assert e["r"]["1M"] is None                     # 마지막 회차 전
    assert e["r"]["3M"] == pytest.approx(100 / 75 - 1)


def test_in_krw_carries_previous_fx_forward():
    idx = pd.bdate_range("2020-01-06", periods=4)
    px = pd.Series([10.0, 11.0, 12.0, 13.0], index=idx)
    fx = pd.Series([1000.0, 1100.0, np.nan, 1300.0], index=idx)   # 셋째 날 환율 없음 → 직전 1100
    assert list(S.in_krw(px, fx)) == [10000.0, 12100.0, 13200.0, 16900.0]
    assert S.in_krw(px, None) is None and S.in_krw(px, pd.Series(np.nan, index=idx)) is None


def test_current_state_values():
    # 35 에서 이벤트(10번째 날) 뒤 27 로 내려옴: 오늘 트리거 거짓, 25 미만이 아니라 무장 안 됨, 9거래일 지남
    x = _ser([15] * 10 + [35] * 5 + [27] * 5)
    c = S.current(x, S._ge(30), S._lt(25))
    assert c["value"] == 27.0 and c["as_of"] == x.index[-1].date().isoformat()
    assert c["active"] is False and c["armed"] is False
    assert c["last_event"] == x.index[10].date().isoformat() and c["days_since_last_event"] == 9
    # 25 미만으로 내려오면 재무장
    y = _ser([15] * 10 + [35] * 5 + [20] * 3)
    assert S.current(y, S._ge(30), S._lt(25))["armed"] is True
    # 오늘 다시 30 이상이면 active(간격 미달이라 이벤트는 아님)
    z = _ser([15] * 10 + [35] * 5 + [20] * 3 + [31])
    cz = S.current(z, S._ge(30), S._lt(25))
    assert cz["active"] is True and cz["days_since_last_event"] == 8
    # 이벤트가 한 번도 없으면 None
    assert S.current(_ser([15] * 5), S._ge(30), S._lt(25))["days_since_last_event"] is None


def test_clean_makes_json_safe():
    o = {1: [np.nan, np.inf, -np.inf, np.float64(1.5), np.int64(3), np.bool_(True), (pd.Timestamp("2020-01-02"), 2.0)]}
    c = S._clean(o)
    assert c == {"1": [None, None, None, 1.5, 3, True, ["2020-01-02", 2.0]]}
    assert [type(v) for v in c["1"][3:6]] == [float, int, bool]
    json.dumps(c, allow_nan=False)


def test_ktb_tr_100bp_drop():
    y = _ser([3.0, 3.0, 2.0])
    tr = S.ktb_tr(y)
    # 둘째 날 이자 3%/252, 셋째 날 이자 + 8 × 1%p + ½ × 0.8 × 1² = 8.4%
    assert tr.iloc[1] / tr.iloc[0] - 1 == pytest.approx(0.03 / 252)
    assert tr.iloc[2] / tr.iloc[1] - 1 == pytest.approx(0.03 / 252 + 0.084)


# ── 공통기간·판정·출력 ──
def _synthetic(move_bad=True):
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2000-01-03", "2014-12-31")
    n = len(idx)

    def spiky(base, amp, every, start):
        v = base + rng.normal(0, 1, n)
        for i in range(start, n, every):
            v[i:i + 10] += amp
        return pd.Series(v, index=idx)

    up = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n))), index=idx)
    move = spiky(80, 90, 200, 50)
    tlt_v = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, n)))
    if move_bad:   # MOVE 급등 뒤 1년간 국채가 빠지게
        for i in np.nonzero(np.diff((move.values >= 120).astype(int)) == 1)[0]:
            tlt_v[i + 1:] *= 0.9
    return {"^VIX": spiky(18, 30, 150, 30), "^MOVE": move, ".KSVKOSPI": spiky(22, 30, 170, 400)[idx >= "2003-01-02"],
            "QQQ": up[idx >= "2001-06-01"], "TLT": pd.Series(tlt_v, index=idx)[idx >= "2002-07-30"],
            "069500.KS": up * 1.1, "KRW=X": pd.Series(1100 + rng.normal(0, 5, n), index=idx),
            S.KTB: pd.Series(5 + np.cumsum(rng.normal(0, 0.02, n)), index=idx)[idx >= "2000-06-01"]}


def test_common_start_is_latest_raw_first_valid_no_warmup():
    s = _synthetic()
    s[S.KTB_TR] = S.ktb_tr(s[S.KTB])
    start, firsts = S.common_start(s)
    vk = s[".KSVKOSPI"]
    # 원자료 첫 유효일 — 1년 백분위 워밍업(252거래일)을 기다리지 않는다
    assert firsts[".KSVKOSPI"] == vk.index[0]
    assert start == max(firsts.values()) == vk.index[0]
    assert firsts["TLT"] == pd.Timestamp("2002-07-30")
    # VKOSPI 가 없으면 그 스터디만 빠지고 나머지 중 가장 늦은 날(TLT)
    s.pop(".KSVKOSPI")
    assert S.common_start(s)[0] == pd.Timestamp("2002-07-30")


def test_compute_output_shape_verdict_and_no_nan():
    d = S.compute(_synthetic(), tranches=S.TRANCHES_EXAMPLE)
    json.dumps(d, allow_nan=False)
    cp = d["meta"]["common_period"]
    assert cp["start"] and ".KSVKOSPI" in cp["binding"]
    assert len(d["studies"]) == len(S.STUDIES) and d["missing"] == []
    for st in d["studies"]:
        assert all(e["signal"] >= cp["start"] for e in st["events"])
        assert [r["h"] for r in st["table"]] == list(S.H)
        assert st["every_day"]["label"] == S.EVERY_DAY
        assert st["every_day"]["n"] >= st["n_events"]
        assert st["longest"]["start"] <= st["start"]
        assert st["tranche_example"]["label"].endswith("참고용)")
        assert {"active", "armed", "last_event", "days_since_last_event"} <= set(st["current"])
        if st["ccy"] == "USD":
            assert st["krw"]["skipped"] is False
    tlt = next(st for st in d["studies"] if st["id"] == "move120_TLT")
    assert tlt["verdict"] == S.NO_EFFECT and tlt["is_signal"] is False
    # 보조 표(longest·krw)에도 판정이 붙고, MOVE 본 표가 '단독 효과 확인 안 됨'이면 같은 판정
    for k in ("longest", "krw"):
        assert (tlt[k]["verdict"], tlt[k]["is_signal"]) == (S.NO_EFFECT, False)
    for st in d["studies"]:
        assert isinstance(st["longest"]["is_signal"], bool) and st["longest"]["verdict"]
        if st["ccy"] == "USD":
            assert isinstance(st["krw"]["is_signal"], bool) and st["krw"]["verdict"]
    assert "252" in cp["note"]
    assert d["meta"]["costs"] == {"round_trip": 0.1, "sensitivity": 0.3, "unit": "%"}


def test_move_verdict_rule():
    good = [_row(h, 10, 10) for h in S.H]
    assert S.move_verdict(good, S.reach_90(good))[1] is True
    weak12 = [_row(h, 10, 10) for h in S.H]
    weak12[3] = _row("12M", 10, 6)
    assert S.move_verdict(weak12, S.reach_90(weak12)) == (S.NO_EFFECT, False)
    assert S.move_verdict([_row(h, 10, 8) for h in S.H], None) == (S.NO_EFFECT, False)   # 12M 80% 지만 90% 도달 안 함
    assert S.verdict("vix30", good, "1M")[1] is True


def test_missing_fx_and_series_are_reported():
    s = _synthetic()
    s.pop("KRW=X")
    s.pop(S.KTB)
    d = S.compute(s)
    json.dumps(d, allow_nan=False)
    assert all(st["krw"]["skipped"] for st in d["studies"] if st["ccy"] == "USD")
    assert {m["target"] for m in d["missing"]} == {S.KTB_TR}
    # 대상(KODEX 200)이 없어도 VKOSPI 의 지금 상태는 남는다
    s.pop("069500.KS")
    d = S.compute(s)
    assert "vkospi40" in d["triggers"] and "active" in d["triggers"]["vkospi40"]["current"]


def test_build_writes_atomic_json(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "OUT", tmp_path / "spikes.json")
    monkeypatch.setattr(S, "load_history", lambda refresh=True: _synthetic())
    d = S.build()
    assert S.load()["meta"]["common_period"] == d["meta"]["common_period"]
    assert S.age_hours() < 1
    assert not (tmp_path / "spikes.json.tmp").exists()
    json.loads((tmp_path / "spikes.json").read_text(encoding="utf-8"))


def test_load_history_reads_csv_and_ktb(tmp_path, monkeypatch):
    import analytics
    idx = pd.bdate_range("2020-01-01", periods=5)
    pd.DataFrame({"^VIX": [20, 21, None, 23, 24], "QQQ": range(5)}, index=idx).rename_axis("date").to_csv(tmp_path / "t.csv")
    monkeypatch.setattr(S, "HIST", tmp_path / "t.csv")
    monkeypatch.setattr(analytics, "load_panel", lambda: pd.DataFrame({"ktb_10y": [3.0, 3.1]}, index=idx[:2]))
    out = S.load_history(refresh=False)
    assert len(out["^VIX"]) == 4 and "TLT" not in out and list(out[S.KTB]) == [3.0, 3.1]
    called = {}
    monkeypatch.setattr(S.R, "refresh_frame", lambda p, t, st, **k: called.update(t=t, st=st) or pd.DataFrame())
    S.load_history()
    assert set(called["t"]) == set(S.TICKERS) | {"^GSPC", "^VIX3M"} and ".KSVKOSPI" in called["t"]
    # ^GSPC(_fill_vix_holes)·^VIX3M(timing.py)는 같은 파일에 유지만 하고 계산 입력으로 돌려주지 않는다
    monkeypatch.setattr(S.R, "refresh_frame", lambda p, t, st, **k: pd.DataFrame({x: [1.0] * 5 for x in t}, index=idx))
    assert "^GSPC" not in S.load_history() and "^VIX3M" not in S.load_history()
