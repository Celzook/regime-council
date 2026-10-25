# -*- coding: utf-8 -*-
"""마켓타이밍 시그널(timing) 회귀 테스트 — 합성 시계열만, Refinitiv·네트워크·data/history 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analytics  # noqa: E402
import spikes  # noqa: E402
import timing as T  # noqa: E402
import valhist  # noqa: E402

CAT = T.load_catalog()
SPEC = {s["key"]: s for s in CAT["signals"]}


def _days(n: int, end: str = "2026-10-23") -> pd.DatetimeIndex:
    return pd.bdate_range(end=end, periods=n)


def _s(values, end: str = "2026-10-23") -> pd.Series:
    values = list(values)
    return pd.Series(values, index=_days(len(values), end), dtype=float)


def _inputs(n: int = 1500, seed: int = 3) -> dict:
    """완비된 합성 입력 — 이름은 load_inputs 와 같다."""
    rng = np.random.default_rng(seed)
    idx = _days(n)
    walk = lambda mu, sd, x0: pd.Series(x0 * np.exp(np.cumsum(rng.normal(mu, sd, n))), index=idx)  # noqa: E731
    return {"vkospi": walk(0, 0.03, 20), "vix": walk(0, 0.03, 18), "vix3m": walk(0, 0.02, 20),
            "kospi200": walk(0.0004, 0.01, 300), "kospi": walk(0.0004, 0.01, 2500),
            "spread_aa": pd.Series(0.6 + np.cumsum(rng.normal(0, 0.005, n)), index=idx),
            "spread_bbb": pd.Series(6.5 + np.cumsum(rng.normal(0, 0.01, n)), index=idx),
            "usdkrw": walk(0, 0.004, 1350), "ks200_basis": pd.Series(rng.normal(0.2, 0.3, n), index=idx)}


def _valhist_doc(pe=None, pb=None) -> dict:
    pe = pe if pe is not None else [10 + (i % 7) for i in range(60)]
    pb = pb if pb is not None else [1 + (i % 5) / 10 for i in range(60)]
    dates = [p.strftime("%Y-%m") for p in pd.period_range("2021-11", periods=len(pe), freq="M")]
    return {"generated_at": "2026-10-26T10:00:00", "items": {"kospi200": {"dates": dates, "pe": pe, "pb": pb}}}


# ── 백분위·z ──
def test_pct_rank_mid_rank_matches_valhist_position():
    vals = [12.0, 10.0, 11.0, 10.0, 13.0, 11.0, 12.0, 11.0]
    # cur = 11: 낮은 값(10) 2개 + 같은 값 3개 × ½ → (2 + 1.5) ÷ 8 = 43.75%
    assert T.pct_rank(pd.Series(vals), 11.0) == pytest.approx(43.75)
    assert valhist.position({"pe": vals}, "pe", 60) == (11.0, round(43.75))


def test_series_stats_windows_are_observation_counts_like_spikes():
    rng = np.random.default_rng(1)
    s = _s(15 + np.cumsum(rng.normal(0, 0.5, 1400)))
    st = T.series_stats(s)
    assert st["pct_1y"] == pytest.approx(spikes.pct_1y(s).iloc[-1])            # 1년 = 최근 252개(spikes 와 같은 정의)
    assert st["pct_5y"] == pytest.approx(T.pct_rank(s.tail(1260), s.iloc[-1]))  # 5년 = 최근 1260개
    assert st["pct_5y_from"] is None                                             # 1260개가 다 찼다
    w1 = s.tail(252)
    assert st["z_1y"] == pytest.approx((s.iloc[-1] - w1.mean()) / w1.std())
    assert st["chg_20d"] == pytest.approx(s.iloc[-1] - s.iloc[-21])


def test_series_stats_monotone_series_top_rank_and_short_history():
    s = _s(range(400))
    st = T.series_stats(s)
    n1 = 252
    assert st["pct_1y"] == pytest.approx((n1 - 1 + 0.5) / n1 * 100)   # 최고값 = (n − 1 + ½) ÷ n
    assert st["pct_5y_from"] is not None                        # 5년이 안 되는 이력은 시작일을 남긴다
    short = T.series_stats(_s(range(30)))
    assert short["pct_1y"] is None and short["pct_5y"] is None   # 60개 이하 → 백분위 없음
    assert short["z_1y"] is not None                             # 21개 이상 → z 는 있다
    assert T.series_stats(_s([5.0] * 100))["z_1y"] is None       # 표준편차 0 → z 없음


def test_transforms():
    s = _s([100.0 + i for i in range(300)])
    gap = T.transform({"kind": "ma_gap", "window": 200}, [s])
    assert gap.iloc[-1] == pytest.approx((399 / np.mean(range(200, 400)) - 1) * 100)
    mom = T.transform({"kind": "ret", "lag": 252, "skip": 21}, [s])
    assert mom.iloc[-1] == pytest.approx((s.iloc[-22] / s.iloc[-253] - 1) * 100)
    chg = T.transform({"kind": "chg_pct", "lag": 20}, [s])
    assert chg.iloc[-1] == pytest.approx((399 / 379 - 1) * 100)
    r = T.transform({"kind": "ratio"}, [_s([20.0, 30.0]), _s([25.0, 0.0])])
    assert list(r) == [pytest.approx(0.8)]                       # 0 나눗셈 날은 뺀다
    assert T.transform({"kind": "level", "scale": 100}, [_s([0.5])]).iloc[-1] == pytest.approx(50)


# ── 상태 판정(방향 포함) ──
def test_vkospi_needs_both_level_and_percentile():
    rule = SPEC["vkospi"]["rule"]
    assert T.judge(rule, {"value": 45, "pct_1y": 95}) == "위험회피"
    assert T.judge(rule, {"value": 45, "pct_1y": 50}) == "중립"      # 40 이상이어도 1년 90% 미만이면 아님(AND)
    assert T.judge(rule, {"value": 15, "pct_1y": 10}) == "위험선호"
    assert T.judge(rule, {"value": 45, "pct_1y": None}) is None      # 백분위 모르면 판정 보류


def test_direction_high_risk_on_vs_off():
    assert SPEC["trend_200d"]["direction"] == "high_risk_on"
    assert T.judge(SPEC["trend_200d"]["rule"], {"value": 3.0}) == "위험선호"
    assert T.judge(SPEC["trend_200d"]["rule"], {"value": -3.0}) == "위험회피"
    assert T.judge(SPEC["trend_200d"]["rule"], {"value": 0.5}) == "중립"
    assert SPEC["vix"]["direction"] == "high_risk_off"
    assert T.judge(SPEC["vix"]["rule"], {"value": 35}) == "위험회피"
    assert T.judge(SPEC["vix"]["rule"], {"value": 12}) == "위험선호"


def test_credit_risk_off_requires_widening():
    rule = SPEC["credit_aa"]["rule"]
    assert T.judge(rule, {"z_1y": 2.5, "chg_20d": 5}) == "위험회피"
    assert T.judge(rule, {"z_1y": 2.5, "chg_20d": -3}) == "중립"      # 높지만 좁혀지는 중
    assert T.judge(rule, {"z_1y": -1.5, "chg_20d": None}) == "위험선호"


def test_vkospi_row_contrarian_note_on_spike():
    base = [18.0 + (i % 10) * 0.5 for i in range(400)]
    row = T.signal_row(SPEC["vkospi"], {"vkospi": _s(base + [55.0])})
    assert row["ok"] and row["state"] == "위험회피" and row["value"] == 55.0
    assert row["contrarian_note"] and "역발상" in row["say"]
    calm = T.signal_row(SPEC["vkospi"], {"vkospi": _s(base + [20.0])})
    assert calm["contrarian_note"] is None


def test_validate_catalog_rejects_direction_mismatch():
    bad = copy.deepcopy(CAT)
    v = next(s for s in bad["signals"] if s["key"] == "vix")
    v["direction"] = "high_risk_on"      # 규칙은 '높으면 회피'인데 방향을 반대로
    assert any("vix" in e and "방향" in e for e in T.validate_catalog(bad))
    bad2 = copy.deepcopy(CAT)
    next(s for s in bad2["signals"] if s["key"] == "credit_aa")["rule"]["risk_on"][0]["lte"] = 3.0   # 선호 임계 > 회피 임계
    assert any("credit_aa" in e for e in T.validate_catalog(bad2))


# ── 카탈로그 ──
def test_catalog_valid_and_covers_implemented_and_phase2():
    assert T.validate_catalog(CAT) == []
    keys = set(SPEC)
    implemented = {"vkospi", "vix", "vix_term", "trend_200d", "mom_6m", "mom_12m", "credit_aa", "credit_bbb",
                   "usdkrw", "valuation_pe", "valuation_pb", "ks200_basis"}
    assert implemented <= keys
    rows = T.compute(_inputs(), CAT, _valhist_doc())["signals"]
    assert {r["key"] for r in rows} == {k for k, s in SPEC.items() if s["status"] in T.IMPLEMENTED}
    phase2 = [s for s in CAT["signals"] if s["status"] == T.PHASE2]
    assert {s["key"] for s in phase2} == {"foreign_futures", "credit_balance", "customer_deposits", "put_call", "arbitrage_balance"}
    for s in phase2:
        assert s["candidates"] and s["definition"] and not s.get("rule") and not s["in_composite"]
    for s in CAT["signals"]:
        assert s["grade"] in "ABC" and s["grade_note"] and s["direction"] in T.DIRECTIONS


# ── 합성 점수 ──
def _row(key, group, state, ok=True, comp=True):
    return {"key": key, "group": group, "state": state, "ok": ok, "in_composite": comp}


def test_composite_group_equal_weight_and_missing():
    rows = [_row("a", "추세", "위험선호"), _row("b", "추세", "중립"), _row("c", "추세", "위험선호"),
            _row("d", "VIX", "위험회피"), _row("e", "크레딧", None, ok=False),
            _row("f", "밸류에이션", "위험회피", comp=False)]
    c = T.composite(rows, CAT)
    # 추세 (1 + 0 + 1) ÷ 3 = 0.667, VIX −1 → (0.667 − 1) ÷ 2 = −0.17 → 중립; 배경 신호는 안 센다
    assert c["score"] == pytest.approx(-0.17) and c["label"] == "중립"
    assert c["n_used"] == 4 and c["n_total"] == 5 and c["n_groups"] == 2 and c["n_groups_total"] == 3
    assert c["missing"] == ["e"]
    empty = T.composite([_row("e", "크레딧", None, ok=False)], CAT)
    assert empty["score"] is None and empty["label"] is None and empty["n_used"] == 0


def test_compute_with_missing_signals_counts_only_available():
    inp = _inputs()
    full = T.compute(inp, CAT, _valhist_doc())
    del inp["vkospi"], inp["vix3m"]
    part = T.compute(inp, CAT, None)
    rows = {r["key"]: r for r in part["signals"]}
    assert not rows["vkospi"]["ok"] and "vkospi" in rows["vkospi"]["reason"]
    assert not rows["vix_term"]["ok"] and "vix3m" in rows["vix_term"]["reason"]
    assert not rows["valuation_pe"]["ok"]
    assert part["composite"]["n_used"] < full["composite"]["n_used"]
    assert {"vkospi", "vix_term"} <= set(part["composite"]["missing"])


def test_trend_falls_back_to_kospi_and_reports_flips():
    inp = _inputs()
    del inp["kospi200"]
    row = T.signal_row(SPEC["trend_200d"], inp)
    assert row["ok"] and row["input_used"] == "kospi" and isinstance(row["flips_1m"], int)


def test_valuation_row_reuses_valhist_position():
    doc = _valhist_doc()
    row = T.signal_row(SPEC["valuation_pe"], {}, doc)
    pe = doc["items"]["kospi200"]["pe"]
    assert row["ok"] and row["value"] == valhist.position({"pe": pe}, "pe", 60)[0]
    assert row["pct_5y"] == valhist.position({"pe": pe}, "pe", 60)[1]
    assert row["pct_1y"] == valhist.position({"pe": pe}, "pe", 12)[1]
    assert row["as_of"] == doc["items"]["kospi200"]["dates"][-1]
    assert row["in_composite"] is False


# ── 견고성·JSON ──
def test_missing_or_broken_data_marks_unavailable_without_crashing():
    d = T.compute({}, CAT, None)
    assert d["meta"]["n_ok"] == 0 and all(not r["ok"] and r["reason"] for r in d["signals"])
    junk = {"vkospi": _s([float("nan")] * 50), "vix": _s([float("inf"), 20.0]), "kospi200": _s([100.0] * 50),
            "usdkrw": pd.Series(["x", "y"], index=_days(2))}
    d2 = T.compute(junk, CAT, {"items": {"kospi200": {"dates": ["2026-10"], "pe": [None]}}})
    rows = {r["key"]: r for r in d2["signals"]}
    assert not rows["vkospi"]["ok"] and not rows["usdkrw"]["ok"] and not rows["valuation_pe"]["ok"]
    assert rows["vix"]["ok"] and rows["vix"]["state"] == "중립" and rows["vix"]["pct_1y"] is None
    assert not rows["trend_200d"]["ok"]                              # 50일로는 200일선 없음


def test_json_has_no_nan_and_meta_fields():
    inp = _inputs()
    inp["spread_aa"].iloc[-5:-2] = np.nan
    inp["kospi200"] = inp["kospi200"].tail(150)                      # 짧은 이력 섞기
    d = T.compute(inp, CAT, _valhist_doc(), as_of={"analytics_panel": "2026-10-23"}, is_sample=True)
    text = json.dumps(d, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text
    assert d["meta"]["is_sample"] is True and d["meta"]["as_of"]["analytics_panel"] == "2026-10-23"
    assert d["generated_at"] == d["meta"]["generated_at"]
    for r in d["signals"]:
        assert r["say"] and r["grade"] and r["source"]
        if r["ok"]:
            assert r["as_of"] and r["value"] is not None
    assert T.to_markdown(d).startswith("## 마켓타이밍")


def test_build_writes_atomically_and_keeps_old_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "OUT", tmp_path / "timing.json")
    monkeypatch.setattr(T, "load_inputs", lambda: (_inputs(), _valhist_doc(), {"analytics_panel": "2026-10-23"}, []))
    d = T.build()
    assert json.loads((tmp_path / "timing.json").read_text(encoding="utf-8"))["meta"]["n_ok"] == d["meta"]["n_ok"] > 0
    assert T.load()["generated_at"] == d["generated_at"] and T.age_hours() < 1
    before = (tmp_path / "timing.json").read_bytes()
    monkeypatch.setattr(T, "load_inputs", lambda: ({}, None, {}, ["analytics 패널: 없음"]))
    with pytest.raises(RuntimeError):
        T.build()
    assert (tmp_path / "timing.json").read_bytes() == before         # 입력이 하나도 없으면 덮어쓰지 않는다
    assert not list(tmp_path.glob("*.tmp"))


def test_load_inputs_without_local_data(tmp_path, monkeypatch):
    def no_panel():
        raise RuntimeError("data/history/ 에 시계열이 없습니다")
    monkeypatch.setattr(analytics, "load_panel", no_panel)
    monkeypatch.setattr(T, "EQ_HIST", tmp_path / "equity_daily.csv")
    monkeypatch.setattr(T, "TIMING_HIST", tmp_path / "timing_daily.csv")
    monkeypatch.setattr(T.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(valhist, "load", lambda: None)
    inputs, vh, as_of, errors = T.load_inputs()
    assert inputs == {} and vh is None and len(errors) == 3


def test_load_inputs_reads_equity_csv_and_market_json(tmp_path, monkeypatch):
    s = _inputs(400)
    monkeypatch.setattr(analytics, "load_panel", lambda: pd.DataFrame({"kospi200": s["kospi200"], "vix": s["vix"]}))
    eq = pd.DataFrame({".KSVKOSPI": s["vkospi"], "^VIX": s["vix"] * 2})
    eq.index.name = "date"
    eq.to_csv(tmp_path / "equity_daily.csv")
    (tmp_path / "market.json").write_text(json.dumps({"meta": {"generated_at": "2026-10-26T16:00"},
                                                      "equity": {"vkospi": {"value": 33.3, "as_of": "2026-10-26"}}}), encoding="utf-8")
    monkeypatch.setattr(T, "EQ_HIST", tmp_path / "equity_daily.csv")
    monkeypatch.setattr(T, "TIMING_HIST", tmp_path / "timing_daily.csv")
    monkeypatch.setattr(T.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(valhist, "load", lambda: None)
    inputs, _, as_of, _ = T.load_inputs()
    assert inputs["vkospi"].iloc[-1] == 33.3 and inputs["vkospi"].index[-1] == pd.Timestamp("2026-10-26")
    assert inputs["vix"].iloc[-1] == pytest.approx(s["vix"].iloc[-1])      # 패널 VIX 우선(equity 열은 대체용)
    assert as_of["analytics_panel"] == "2026-10-23" and as_of["market_json"] == "2026-10-26T16:00"


# ── 리뷰 반영(2026-10-26) ──
def test_validate_catalog_contrarian_note_and_score_states():
    bad = copy.deepcopy(CAT)
    next(s for s in bad["signals"] if s["key"] == "vix")["contrarian"]["note"] = "  "
    assert any("vix.contrarian" in e and "note" in e for e in T.validate_catalog(bad))
    bad2 = copy.deepcopy(CAT)
    del bad2["composite"]["score"]["중립"]
    assert any("composite.score" in e for e in T.validate_catalog(bad2))
    bad3 = copy.deepcopy(CAT)
    bad3["composite"]["score"]["위험회피"] = "-1"
    assert any("composite.score" in e for e in T.validate_catalog(bad3))


def test_signal_row_turns_any_error_into_ok_false(monkeypatch):
    spec = copy.deepcopy(SPEC["vix"])
    del spec["rule"]                                                   # 판정 단계에서 KeyError 가 나는 신호
    row = T.signal_row(spec, {"vix": _s([20.0] * 300)})
    assert row["ok"] is False and row["judged"] is False and row["reason"].startswith("계산 오류")
    assert row["value"] is None and row["say"]

    def boom(rule, m):
        raise ValueError("규칙 깨짐")
    monkeypatch.setattr(T, "judge", boom)
    d = T.compute(_inputs(), CAT, _valhist_doc())                      # compute() 는 죽지 않는다
    assert all(not r["ok"] and r["reason"].startswith("계산 오류") for r in d["signals"])


def test_pick_falls_back_when_primary_present_but_short():
    inp = _inputs()
    inp["kospi200"] = inp["kospi200"].tail(150)                        # 있지만 200일선을 못 만든다
    row = T.signal_row(SPEC["trend_200d"], inp)
    assert row["ok"] and row["judged"] and row["input_used"] == "kospi"
    full = T.signal_row(SPEC["trend_200d"], _inputs())
    assert full["input_used"] == "kospi200"                            # 충분하면 1순위 그대로


def test_pick_needs_every_rule_metric_not_just_a_value():
    """1순위가 변환값은 있지만 규칙이 쓰는 지표(1년 백분위)를 못 만들면 다음 후보로."""
    import copy
    spec = copy.deepcopy(SPEC["mom_6m"])
    spec["rule"] = {"risk_on": [{"m": "pct_1y", "gte": 80}], "risk_off": [{"m": "pct_1y", "lte": 20}]}
    rng = np.random.default_rng(1)
    k200 = pd.Series(300 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))), index=pd.bdate_range(end="2026-10-23", periods=300))
    kospi = pd.Series(2500 * np.exp(np.cumsum(rng.normal(0, 0.01, 1500))), index=pd.bdate_range(end="2026-10-23", periods=1500))
    assert T.signal_row(spec, {"kospi200": k200, "kospi": kospi})["input_used"] == "kospi"


def test_vkospi_pct_1y_matches_spikes_definition():
    # 달력 1년(≈261 영업일)과 최근 252개가 갈리게: 252개 바로 앞 9개만 아주 높다
    vals = [10.0] * 39 + [100.0] * 9 + [float(i % 100) for i in range(251)] + [50.0]
    s = _s(vals)
    row = T.signal_row(SPEC["vkospi"], {"vkospi": s})
    assert row["pct_1y"] == round(spikes.pct_1y(s).iloc[-1])
    short = T.signal_row(SPEC["vkospi"], {"vkospi": s.tail(251)})
    assert short["pct_1y"] is None and np.isnan(spikes.pct_1y(s.tail(251)).iloc[-1])   # 252개 전엔 둘 다 없음


def test_judge_on_displayed_rounded_value():
    row = T.signal_row(SPEC["vix"], {"vix": _s([18.0 + (i % 5) for i in range(300)] + [29.996])})
    assert row["value"] == 30.0 and row["state"] == "위험회피"          # 보이는 30.0 과 상태가 같다
    assert row["contrarian_note"] and "30.0" in row["say"]


def test_ok_vs_judged_contract():
    row = T.signal_row(SPEC["vkospi"], {"vkospi": _s([30.0 + (i % 7) for i in range(99)] + [45.0])})
    assert row["ok"] is True and row["value"] == 45.0                  # 값은 계산했지만
    assert row["judged"] is False and row["state"] is None and "보류" in row["say"]   # 1년 백분위가 없어 판정 보류
    full = T.compute(_inputs(), CAT, _valhist_doc())
    for r in full["signals"]:
        assert r["judged"] == (r["state"] is not None) and (not r["judged"] or r["ok"])
    assert "보류" in T.to_markdown({**full, "signals": [row]})


def test_composite_boundary_strict_on_displayed_score():
    third = [_row("a", "추세", "위험선호"), _row("b", "크레딧", "중립"), _row("c", "환율", "중립")]
    c = T.composite(third, CAT)
    assert c["score"] == 0.33 and c["label"] == "중립"                   # 정확히 1/3 → 0.33 → 경계 위(엄격 >) 아님
    neg = [_row("a", "추세", "위험회피"), _row("b", "크레딧", "중립"), _row("c", "환율", "중립")]
    assert T.composite(neg, CAT)["label"] == "중립"
    half = [_row("a", "추세", "위험선호"), _row("b", "크레딧", "중립")]
    assert T.composite(half, CAT)["label"] == "위험선호"
    assert "엄격" in CAT["composite"]["note"]


def test_composite_excludes_waiting_signals_and_merges_us_volatility():
    rows = [_row("a", "추세", "위험선호"), {**_row("w", "미국 변동성", None, ok=False), "status": T.WAITING}]
    c = T.composite(rows, CAT)
    assert c["n_total"] == 1 and c["n_groups_total"] == 1 and c["waiting"] == ["w"] and c["missing"] == []
    assert c["label"] == "위험선호"
    assert SPEC["vix"]["group"] == SPEC["vix_term"]["group"] == "미국 변동성"
    assert SPEC["vix_term"]["status"] == "구현"
    assert "미국 변동성" in CAT["meta"]["note"]


def test_stale_signal_shown_but_excluded_from_composite():
    inp = _inputs()
    inp["spread_aa"] = inp["spread_aa"].iloc[:-10]                     # 10영업일 늦은 원천
    inp["spread_bbb"] = inp["spread_bbb"].iloc[:-5]                    # 5영업일 = 경계 안
    old = _valhist_doc(pe=[10 + (i % 7) for i in range(58)], pb=[1 + (i % 5) / 10 for i in range(58)])   # 2026-08 까지
    d = T.compute(inp, CAT, old)
    rows = {r["key"]: r for r in d["signals"]}
    aa = rows["credit_aa"]
    assert aa["stale"] is True and aa["ok"] and aa["value"] is not None and "지연" in aa["reason"]
    assert rows["credit_bbb"]["stale"] is False and rows["vix"]["stale"] is False
    assert rows["valuation_pe"]["stale"] is True                       # 2026-08 말 → 10-23 은 45일 초과
    c = d["composite"]
    assert c["stale"] == ["credit_aa"] and "credit_aa" not in c["missing"]
    assert c["n_used"] == c["n_total"] - 1
    fresh = T.compute(_inputs(), CAT, _valhist_doc(pe=[10 + (i % 7) for i in range(59)], pb=[1.0] * 59))   # 2026-09
    assert not any(r["stale"] for r in fresh["signals"]) and fresh["composite"]["stale"] == []


def test_history_from_is_raw_input_start_and_valhist_short_window():
    inp = _inputs()
    row = T.signal_row(SPEC["trend_200d"], inp)
    assert row["history_from"] == inp["kospi200"].index[0].date().isoformat()   # 200일선 계산 전 원 입력 시작
    doc = _valhist_doc(pe=[10 + (i % 7) for i in range(30)], pb=[1.0] * 30)
    v = T.signal_row(SPEC["valuation_pe"], {}, doc)
    assert v["pct_5y_from"] == doc["items"]["kospi200"]["dates"][0]           # 60개월 중 30개뿐
    assert T.signal_row(SPEC["valuation_pe"], {}, _valhist_doc())["pct_5y_from"] is None


def test_ks200_basis_note_flags_raw_basis():
    assert SPEC["ks200_basis"]["threshold_note"].startswith("원 베이시스 z — 만기 톱니·배당락 미보정(v2: 이론 베이시스 차감)")


def test_flips_1m_counts_last_21_days_exactly():
    px = _s([100.0] * 300 + [101.0 if i % 2 == 0 else 99.0 for i in range(21)])
    row = T.signal_row(SPEC["trend_200d"], {"kospi200": px})
    assert row["flips_1m"] == 21                                       # 선 위로 1번 + 그 뒤 20번 교대
    calm = T.signal_row(SPEC["trend_200d"], {"kospi200": _s([100.0 + i * 0.1 for i in range(330)])})
    assert calm["flips_1m"] == 0


def _vkospi_files(tmp_path, monkeypatch, as_of_text):
    s = _inputs(400)
    monkeypatch.setattr(analytics, "load_panel", lambda: pd.DataFrame({"kospi200": s["kospi200"]}))
    eq = pd.DataFrame({".KSVKOSPI": s["vkospi"]})
    eq.index.name = "date"
    eq.to_csv(tmp_path / "equity_daily.csv")
    (tmp_path / "market.json").write_text(json.dumps({"equity": {"vkospi": {"value": 99.9, "as_of": as_of_text}}}), encoding="utf-8")
    monkeypatch.setattr(T, "EQ_HIST", tmp_path / "equity_daily.csv")
    monkeypatch.setattr(T, "TIMING_HIST", tmp_path / "timing_daily.csv")
    monkeypatch.setattr(T.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(valhist, "load", lambda: None)
    return s["vkospi"]


def test_market_json_vkospi_not_appended_when_not_newer(tmp_path, monkeypatch):
    for as_of_text in ("2026-10-23", "2026-10-23T15:45:00", "2026-10-20"):   # CSV 마지막 날 = 2026-10-23(시각이 붙어도 같은 날)
        hist = _vkospi_files(tmp_path, monkeypatch, as_of_text)
        inputs, _, _, _ = T.load_inputs()
        assert len(inputs["vkospi"]) == len(hist), as_of_text
        assert inputs["vkospi"].iloc[-1] == pytest.approx(hist.iloc[-1])
    _vkospi_files(tmp_path, monkeypatch, "2026-10-26T09:00:00")      # 더 최신이면 날짜로 잇는다
    inputs, _, _, _ = T.load_inputs()
    assert inputs["vkospi"].iloc[-1] == 99.9 and inputs["vkospi"].index[-1] == pd.Timestamp("2026-10-26")


def test_load_inputs_reads_vix3m_from_timing_daily(tmp_path, monkeypatch):
    s = _inputs(400)
    monkeypatch.setattr(analytics, "load_panel", lambda: pd.DataFrame({"vix": s["vix"]}))
    eq = pd.DataFrame({".KSVKOSPI": s["vkospi"]})
    eq.index.name = "date"
    eq.to_csv(tmp_path / "equity_daily.csv")
    td = pd.DataFrame({"^VIX": s["vix"] * 2, "^VIX3M": s["vix3m"], ".KSVKOSPI": s["vkospi"] + 1})
    td.index.name = "date"
    td.to_csv(tmp_path / "timing_daily.csv")
    monkeypatch.setattr(T, "EQ_HIST", tmp_path / "equity_daily.csv")
    monkeypatch.setattr(T, "TIMING_HIST", tmp_path / "timing_daily.csv")
    monkeypatch.setattr(T.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(valhist, "load", lambda: None)
    inputs, _, as_of, errors = T.load_inputs()
    assert inputs["vix3m"].iloc[-1] == pytest.approx(s["vix3m"].iloc[-1]) and as_of["timing_daily"] == "2026-10-23"
    assert inputs["vix"].iloc[-1] == pytest.approx(s["vix"].iloc[-1])           # 패널 VIX 우선
    assert inputs["vkospi"].iloc[-1] == pytest.approx(s["vkospi"].iloc[-1])     # 같은 날은 equity_daily 값
    row = T.signal_row(SPEC["vix_term"], inputs)
    assert row["ok"] and row["input_used"] == "vix÷vix3m"
