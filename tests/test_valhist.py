# -*- coding: utf-8 -*-
"""밸류에이션 이력(valhist) 묶음 계산 회귀 테스트 — Refinitiv·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import datetime
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import valhist as V  # noqa: E402


def test_harmonic_is_cap_over_earnings():
    # 60·40 비중, PER 10·20 → 100 ÷ (6 + 2) = 12.5 (산술평균 14 가 아니다)
    assert V.harmonic([(60, 10), (40, 20)]) == pytest.approx(12.5)


def test_harmonic_drops_negative_zero_missing_pe():
    # fundamentals.basket 과 같게 PER ≤ 0·없음·NaN 은 빼고 남은 비중으로만
    assert V.harmonic([(50, 10), (30, -5), (10, 0), (5, None), (5, float("nan"))]) == pytest.approx(10)
    assert V.harmonic([(50, -3)]) is None


def test_basket_month_drifts_weights_by_price():
    picked = [("A", 50.0), ("B", 50.0)]
    now = {"A": 100.0, "B": 100.0}
    # 가격이 지금과 같으면 지금 비중(반반) 그대로; 과거 A 가격이 절반이었으면 A 비중이 25:50 으로 줄어든다
    pe, pb, pe_cov, pb_cov = V.basket_month(picked, {"A": (100.0, 10.0, 1.0), "B": (100.0, 20.0, 2.0)}, now)
    assert pe == pytest.approx(100 / (50 / 10 + 50 / 20))
    assert pb == pytest.approx(100 / (50 / 1 + 50 / 2))
    assert pe_cov == pb_cov == pytest.approx(100)
    pe, *_ = V.basket_month(picked, {"A": (50.0, 10.0, 1.0), "B": (100.0, 20.0, 2.0)}, now)
    assert pe == pytest.approx(75 / (25 / 10 + 50 / 20))


def test_basket_month_negative_pe_keeps_pb():
    pe, pb, pe_cov, _ = V.basket_month([("A", 30.0), ("B", 70.0)], {"A": (10.0, -4.0, 1.0), "B": (10.0, 20.0, 2.0)}, {"A": 10.0, "B": 10.0})
    assert pe == pytest.approx(20) and pe_cov == pytest.approx(70)   # 적자 A 는 PER 에서만 빠진다
    assert pb == pytest.approx(100 / (30 / 1 + 70 / 2))


def test_basket_month_blank_metric_when_valid_weight_under_60pct():
    # 적자 A 가 비중 50% → PER 쓸 만한 비중 50% < 60% 라 PER 만 비우고, PBR 은 그대로
    pe, pb, pe_cov, pb_cov = V.basket_month([("A", 50.0), ("B", 50.0)], {"A": (10.0, -4.0, 1.0), "B": (10.0, 20.0, 2.0)}, {"A": 10.0, "B": 10.0})
    assert pe is None and pe_cov == pytest.approx(50)
    assert pb == pytest.approx(100 / (50 / 1 + 50 / 2)) and pb_cov == pytest.approx(100)


def test_basket_month_blank_when_most_weight_unpriced():
    picked = [("A", 70.0), ("B", 30.0)]
    pe, pb, pe_cov, _ = V.basket_month(picked, {"B": (10.0, 15.0, 1.5)}, {"A": 5.0, "B": 10.0})   # 30% < 절반
    assert pe is None and pb is None and pe_cov == pytest.approx(30)
    pe, *_ = V.basket_month(picked, {"A": (5.0, 8.0, 1.0)}, {"A": 5.0, "B": 10.0})                 # 70% ≥ 절반
    assert pe == pytest.approx(8)


def test_clean_row_pref_scaled_by_price_and_absurd_ratios_dropped():
    pref, com = next(iter(V.F.PREF.items()))
    row = V.clean_row({pref: (80.0, 10.0, 2.0), com: (100.0, 10.0, 2.0), "X": (5.0, 0.4, 0.01), "Y": (5.0, 12.0, 1.1)})
    assert row[pref] == (80.0, pytest.approx(8.0), pytest.approx(1.6))   # 보통주 PER·PBR × 80/100
    assert row["X"] == (5.0, None, None)                                    # PER < 1·PBR < 0.05 는 원천 오류로 뺀다
    assert row["Y"] == (5.0, 12.0, 1.1)
    assert V.clean_row({pref: (80.0, 10.0, 2.0)})[pref] == (80.0, None, None)   # 보통주 값이 없으면 쓰지 않는다


def test_position_matches_screen_percentile():
    it = {"pe": [None] * 3 + [10.0, 12.0, 14.0, 16.0, 18.0, 14.0], "pb": [1.0] * 5}
    assert V.position(it, "pe") == (14.0, 50)       # 낮았던 2달 + 같은 2달 × ½ = 3 / 6
    assert V.position(it, "pb") is None             # 6개월 미만
    assert V.position_text({"items": {"k": {**it, "n": 50}}}, "k") == "PER 14.0배(구성 상위 90% 기준) 5년 중 50%"
    assert V.position_text(None, "k") == "-"


# ── update_cache 증분(네트워크 없음 — Refinitiv 호출을 가짜로) ──
@pytest.fixture
def fake_net(tmp_path, monkeypatch):
    """R.snapshot(월말 이력) — 요청 기간의 매달 (가격 100, PER 10, PBR 1). fail 에 든 종목이 있는 묶음은 예외.
    F._rows(스냅샷) — 지금 값 (101, 11, 1.1). calls 에 (종목들, SDate) 를 남긴다."""
    st = {"fail": set(), "calls": []}
    monkeypatch.setattr(V, "CACHE", tmp_path / "val_monthly.csv")
    monkeypatch.setattr(V, "BATCH", 1)

    def snapshot(rics, fields, params):
        st["calls"].append((tuple(rics), params["SDate"]))
        if st["fail"] & set(rics):
            raise RuntimeError("timeout")
        ms = V.months(params["SDate"], datetime.date.fromisoformat(params["EDate"]))
        return pd.DataFrame([(r, m + "-28", 100.0, 10.0, 1.0) for r in rics for m in ms]), None
    monkeypatch.setattr(V.R, "snapshot", snapshot)
    monkeypatch.setattr(V.F, "_rows", lambda rics, f: {r: [101.0, 11.0, 1.1] for r in rics})
    return st


def test_update_cache_refetches_ticker_whose_history_failed(fake_net):
    fake_net["fail"] = {"B"}
    df, skipped = V.update_cache(["A", "B"], today=datetime.date(2026, 10, 24))
    assert skipped == ["B"]
    assert df[df.ric == "B"].month.tolist() == ["2026-10"]          # 이력 실패 — 스냅샷 행만
    assert (df[df.ric == "A"].month.str[:4] == "2016").sum() == 12

    fake_net["fail"], fake_net["calls"] = set(), []
    df, skipped = V.update_cache(["A", "B"], today=datetime.date(2026, 11, 5))
    starts = dict((r[0], s) for r, s in fake_net["calls"])
    assert skipped == [] and starts == {"A": "2026-09-01", "B": V.START}   # A 는 마지막 월말 이력(9월)부터, B 는 스냅샷 행이 있어도 처음부터
    b = df[df.ric == "B"]
    assert b.month.iloc[0] == "2016-01" and len(b) == len(V.months(V.START, datetime.date(2026, 11, 5)))
    a10 = df[(df.ric == "A") & (df.month == "2026-10")].iloc[0]
    assert a10.price == 100.0 and a10.snap == 0                       # 지난달 스냅샷(101)은 월말 값으로 바뀐다
    assert df[(df.month == "2026-11")].snap.tolist() == [1, 1]


def test_update_cache_snapshot_month_is_not_history_after_repeated_failure(fake_net):
    V.update_cache(["A"], today=datetime.date(2026, 9, 10))
    fake_net["fail"] = {"A"}
    V.update_cache(["A"], today=datetime.date(2026, 10, 10))        # 9월 월말 재받기 실패 → 10월은 스냅샷만
    fake_net["fail"], fake_net["calls"] = set(), []
    df, _ = V.update_cache(["A"], today=datetime.date(2026, 11, 10))
    assert fake_net["calls"] == [(("A",), "2026-08-01")]             # 스냅샷(9·10월)이 아니라 마지막 월말 이력(8월)부터
    assert df[df.month.isin(["2026-09", "2026-10"])].snap.tolist() == [0, 0]


def test_update_cache_groups_tickers_by_start_month(fake_net, monkeypatch):
    V.update_cache(["A", "B"], today=datetime.date(2026, 9, 10))
    V.update_cache(["A"], today=datetime.date(2026, 10, 10))         # A 월말 이력은 9월까지, B 는 8월에서 멈춤
    fake_net["calls"] = []
    V.update_cache(["A", "B", "C"], today=datetime.date(2026, 11, 10))
    assert sorted(fake_net["calls"]) == [(("A",), "2026-09-01"), (("B",), "2026-08-01"), (("C",), V.START)]


def test_pick_stops_at_cumulative_weight_and_cap():
    hs = [{"code": c, "w": w} for c, w in [("C", 10), ("A", 50), ("B", 30), ("D", 5), ("E", 5), ("X", None)]]
    p, cov = V.pick(hs, cum=90)
    assert [c for c, _ in p] == ["A", "B", "C"] and cov == pytest.approx(90)
    p, cov = V.pick(hs, cum=90, cap=2)
    assert [c for c, _ in p] == ["A", "B"] and cov == pytest.approx(80)


def test_months_run_to_current_month():
    m = V.months("2016-01-01", datetime.date(2026, 10, 24))
    assert m[0] == "2016-01" and m[-1] == "2026-10" and len(m) == 130
