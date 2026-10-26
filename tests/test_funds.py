# -*- coding: utf-8 -*-
"""운용사·펀드 스크리닝(fund_universe·funds) 회귀 테스트 — 합성 XML·합성 값만, 금투협·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import datetime as dt
import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import fund_universe as U  # noqa: E402
import funds as F  # noqa: E402
import web  # noqa: E402

CFG = U.load_cfg()


def _xml(rows, tag="selectMeta"):
    metas = "".join(f"<{tag}><vGridBtn1 />" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + f"</{tag}>" for r in rows)
    return f'<?xml version="1.0"?><root><message><L>{metas}</L></message></root>'


# ── 클래스 묶기·분류 ──
@pytest.mark.parametrize("name,stem,cls", [
    ("가나미국S&P500인덱스증권자투자신탁(주식-파생형)(H)C-E", "가나미국S&P500인덱스증권자투자신탁(주식-파생형)(H)", "C-E"),
    ("가나코리아증권자투자신탁 1[주식]ClassC/C-G", "가나코리아증권자투자신탁 1[주식]", "C-G"),
    ("가나코리아증권자투자신탁[주식]ClassC(Pe)1", "가나코리아증권자투자신탁[주식]", "CPe1"),
    ("가나배당증권자투자신탁(주식)C-P2e형", "가나배당증권자투자신탁(주식)", "C-P2e"),
    ("가나10년증권투자신탁 1(주식)(C-e)", "가나10년증권투자신탁 1(주식)", "C-e"),
    ("가나인덱스증권자투자신탁 1(주식)ClassC(장마)", "가나인덱스증권자투자신탁 1(주식)", "C"),
    ("가나글로벌증권자투자신탁(UH)[주식]I클래스", "가나글로벌증권자투자신탁(UH)[주식]", "I"),
    ("가나그룹증권자투자신탁 1(주식)", "가나그룹증권자투자신탁 1(주식)", ""),
    ("가나코리아펀드ClassCe", "가나코리아펀드", "Ce"),
])
def test_split_class(name, stem, cls):
    assert U.split_class(name) == (stem, cls)


def test_umbrella_hedge_exclusion():
    assert U.is_umbrella("가나증권자투자신탁(주식)운용") and U.is_umbrella("가나증권자투자신탁[주식](운용)")
    assert not U.is_umbrella("가나운용증권자투자신탁[주식]C-e")
    assert U.hedge_of("가나미국증권자투자신탁(주식-파생형)(H)C-E") == "H" and U.hedge_of("가나미국증권자투자신탁UH[주식]") == "UH"
    assert U.hedge_of("가나차이나H인덱스증권자투자신탁(주식)") is None
    assert U.excluded("가나200증권상장지수투자신탁(주식)", "주식형", CFG) == "ETF"
    assert U.excluded("가나TDF2040증권투자신탁(주식혼합)", "주식형", CFG).startswith("재간접")
    assert U.excluded("가나증권투자신탁(주식)", "재간접형", CFG).startswith("재간접")
    assert U.excluded("가나코리아증권자투자신탁(주식)", "주식형", CFG) is None


def test_classify_official_subtype_with_refinement():
    assert U.classify("111", "가나고배당증권투자신탁(주식)", "-", CFG)["group"] == "배당"
    assert U.classify("111", "가나코리아증권투자신탁(주식)", "-", CFG)["group"] == "일반"
    assert U.classify("112", "가나증권투자신탁(주식)", "-", CFG)["group"] == "성장"
    t = U.classify("113", "가나반도체증권투자신탁(주식)", "-", CFG)
    assert (t["family"], t["group"], t["theme"]) == ("kr_active", "테마·업종", "반도체·IT")
    assert U.classify("114", "가나증권투자신탁(주식)", "-", CFG)["family"] == "kr_index"
    assert U.classify("181", "가나롱숏증권투자신탁(주식-파생형)", "-", CFG) is None          # 파생형은 '인덱스'만
    g = U.classify("214", "가나미국인덱스증권투자신탁(주식)", "북미", CFG)
    assert (g["group"], g["index"]) == ("미국", True)
    assert U.classify("211", "가나증권투자신탁(주식)", "남극", CFG)["group"] == CFG["other_region"]
    assert U.classify("213", "가나헬스케어증권투자신탁(주식)", "글로벌", CFG)["group"] == "테마·섹터"
    assert U.classify("999", "가나", "-", CFG) is None


def _snap_row(name, mgr_cd="M1", units=1000.0, nav=1000.0, setup="2015-01-01", typ="주식형"):
    return {"name": name, "mgr": "가나운용", "mgr_cd": mgr_cd, "type": typ, "setup": setup, "units": units, "nav": nav}


def test_universe_build_groups_classes_and_screens():
    stem = "가나코리아증권자투자신탁(주식)"
    snap = {
        "A1": _snap_row(stem + "ClassA", units=6000), "A2": _snap_row(stem + "ClassC-e", units=3000),
        "A3": _snap_row(stem + "ClassS", units=1000, setup="2026-01-01"), "A9": _snap_row(stem + "운용", units=99999),
        "B1": _snap_row("가나소형증권투자신탁(주식)C-e", units=500),                                   # 5억 → 미달
        "C1": _snap_row("가나새증권투자신탁(주식)C-e", units=50000, setup="2026-06-01"),               # 1년 미만
        "D1": _snap_row("가나200증권상장지수투자신탁(주식)", units=90000),                             # ETF
        "E1": _snap_row("가나무분류증권투자신탁(주식)", units=90000),                                  # 공식 분류 없음
        "G1": _snap_row("가나혼합증권투자신탁(채권혼합-파생형)", units=90000, typ="혼합채권파생형"),
        "H1": _snap_row("나다미국증권자투자신탁(주식)C-e", units=8000, setup="2026-03-01"),            # C-e 가 1년 미만
        "H2": _snap_row("나다미국증권자투자신탁(주식)A", units=4000, setup="2016-01-01"),
    }
    rop = {"111": [{"standardCd": c} for c in ("A1", "A2", "B1", "C1")],
           "211": [{"standardCd": "H2", "vInvestRgnGbNm": "북미"}]}
    u = U.build(rop, snap, dt.date(2026, 10, 26), CFG)
    by = {f["name"]: f for f in u["funds"]}
    a = by[stem]
    assert a["classes"] == ["A1", "A2", "A3"] and a["rep_code"] == "A2" and a["rep_is_ce"]   # 운용 행은 합계에서 뺀다
    assert a["aum_eok"] == pytest.approx(100.0) and a["setup"] == "2015-01-01" and a["group_id"] == "kr_active:일반"
    h = by["나다미국증권자투자신탁(주식)"]
    assert h["rep_code"] == "H2" and not h["rep_is_ce"] and h["group_id"] == "global:미국"
    assert set(by) == {stem, "나다미국증권자투자신탁(주식)"}
    ex = u["excluded"]
    assert ex["ETF"] == 1 and ex["운용 계정(클래스 합과 중복)"] == 1 and ex["합산 순자산 기준 미달"] == 1
    assert ex["설정 1년 미만"] == 1 and ex["금투협 소유형 없음 — 100억 이상"] == 1 and sum(v for k, v in ex.items() if "대유형" in k) == 1
    assert u["counts"] == {"global:미국": 1, "kr_active:일반": 1}


def _m(code, cls, aum):
    return {"code": code, "cls": cls, "aum_eok": aum}


def test_operating_accounts():
    # 꼬리 없는 행 = 운용 계정(클래스가 여럿일 때)
    assert U.operating_accounts([_m("U", "", 300), _m("A", "A", 100), _m("C", "C-e", 200)]) == {"U"}
    # 이름으로 못 찾았을 때만: 다른 클래스 합과 0.5% 안으로 같으면 운용 계정(꼬리가 있어도), '모' 꼬리도 운용 계정
    assert U.operating_accounts([_m("X", "C1", 301), _m("A", "A", 100), _m("C", "C-e", 150), _m("S", "S", 50)]) == {"X"}
    assert U.operating_accounts([_m("X", "C1", 310), _m("A", "A", 100), _m("C", "C-e", 150), _m("S", "S", 50)]) == set()
    assert U.operating_accounts([_m("X", "C1", 301), _m("A", "A", 100), _m("C", "C-e", 150), _m("S", "S", 50)], named=True) == set()
    assert U.operating_accounts([_m("M", "모", 300), _m("A", "A", 100), _m("C", "C-e", 200)]) == {"M"}
    # 꼬리 없는 행을 뺀 뒤 가장 큰 판매 클래스가 우연히 나머지 합과 같아도 빼지 않는다
    assert U.operating_accounts([_m("U", "", 400), _m("C", "C-e", 200), _m("A", "A", 100), _m("S", "S", 100)]) == {"U"}
    # 클래스 하나뿐인 펀드는 그대로, 둘뿐이면 합 규칙은 쓰지 않는다, 아주 작은 합은 잡음
    assert U.operating_accounts([_m("U", "", 300)]) == set()
    assert U.operating_accounts([_m("A", "A", 100), _m("C", "C-e", 100)]) == set()
    assert U.operating_accounts([_m("A", "A", 1), _m("C", "C-e", 0.5), _m("S", "S", 0.5)]) == set()


def test_universe_drops_operating_accounts_and_never_picks_them():
    stem = "가나글로벌증권자투자신탁(주식)"
    snap = {"U1": _snap_row(stem, units=20000), "A1": _snap_row(stem + "ClassA", units=12000), "S1": _snap_row(stem + "ClassS", units=8000),
            "W1": _snap_row("나다증권투자신탁[주식](운)", units=9000, mgr_cd="M2"), "W2": _snap_row("나다증권투자신탁[주식]A", units=9000, mgr_cd="M2"),
            "O1": _snap_row("라마단일증권투자신탁(주식)", units=30000)}
    rop = {"211": [{"standardCd": c, "vInvestRgnGbNm": "글로벌"} for c in ("U1", "A1", "S1")],
           "111": [{"standardCd": c} for c in ("W1", "W2", "O1")]}
    u = U.build(rop, snap, dt.date(2026, 10, 26), CFG)
    by = {f["name"]: f for f in u["funds"]}
    g = by[stem]
    assert g["classes"] == ["A1", "S1"] and g["rep_code"] == "A1" and g["aum_eok"] == pytest.approx(200.0)
    assert "나다증권투자신탁[주식]" not in by                       # (운) 을 빼면 90억 → 미달
    assert by["라마단일증권투자신탁(주식)"]["classes"] == ["O1"]     # 클래스 하나뿐인 펀드는 남는다
    assert u["excluded"]["운용 계정(클래스 합과 중복)"] == 2


def test_notation_variants_group_together():
    """같은 펀드가 표기만 다를 때('(주식)'/'[주식]', 공백, 밑줄) 한 묶음 — 꼬리 없는 운용 계정이 단일 펀드로 남지 않는다."""
    assert U.stem_key("가나연금증권자투자신탁 1(주식)") == U.stem_key("가나연금증권자투자신탁1[주식]") == U.stem_key("가나연금증권자투자신탁_1［주식］")
    snap = {"U": _snap_row("가나연금증권자투자신탁 1(주식)", units=30000), "C": _snap_row("가나연금증권자투자신탁 1[주식]Ce", units=20000),
            "A": _snap_row("가나연금증권자투자신탁1[주식]A", units=10000)}
    u = U.build({"111": [{"standardCd": c} for c in snap]}, snap, dt.date(2026, 10, 26), CFG)
    (f,) = u["funds"]
    assert f["classes"] == ["A", "C"] and f["rep_code"] == "C" and f["aum_eok"] == pytest.approx(300.0)
    assert u["excluded"]["운용 계정(클래스 합과 중복)"] == 1


# ── 금투협 XML 파싱 ──
def test_snap_rows_and_nav_rows():
    rows = F.parse(_xml([{"tmpV1": "가&amp;나", "tmpV2": "펀드A", "tmpV3": "주식형", "tmpV4": "20100104", "tmpV5": "300", "tmpV6": "1010.5",
                          "tmpV12": "X1", "tmpV13": "M1"},
                         {"tmpV2": "펀드B", "tmpV6": "0", "tmpV12": "X2"}]))
    s = F.snap_rows(rows)
    assert list(s) == ["X1"] and s["X1"] == {"name": "펀드A", "mgr": "가&나", "mgr_cd": "M1", "type": "주식형", "setup": "2010-01-04",
                                              "units": 300.0, "nav": 1010.5}
    nav = F.nav_rows(F.parse(_xml([{"tmpV1": "20200103", "tmpV2": "1010.5", "tmpV5": "300"}, {"tmpV1": "20200102", "tmpV2": "0"}])))
    assert list(nav) == ["2020-01-03"]


def test_dist_rows_keep_only_real_distributions():
    rows = F.parse(_xml([{"tmpV4": "20200630", "tmpV7": "1200", "tmpV9": "1000", "tmpV11": "X1"},
                         {"tmpV4": "20200630", "tmpV7": "1100", "tmpV9": "1100", "tmpV11": "X2"},
                         {"tmpV4": "20200630", "tmpV7": "1100", "tmpV9": "0", "tmpV11": "X3"}]))
    assert F.dist_rows(rows) == [{"code": "X1", "end": "2020-06-30", "before": 1200.0, "after": 1000.0}]


def test_adjust_applies_factor_after_settlement_end():
    hist = [{"date": "2020-06-29", "nav": 1190.0, "units_mn": 100.0}, {"date": "2020-06-30", "nav": 1200.0, "units_mn": 100.0},
            {"date": "2020-07-01", "nav": 1000.0, "units_mn": 120.0}, {"date": "2020-07-02", "nav": 1010.0, "units_mn": 120.0}]
    out = F.adjust(hist, [("2020-06-30", 1200.0, 1000.0)])
    assert [round(r["adj_nav"], 2) for r in out] == [1190.0, 1200.0, 1200.0, 1212.0]


def test_fee_rows_maps_fields_and_ter():
    rows = F.parse(_xml([{"tmpV5": ".3", "tmpV6": ".2", "tmpV7": ".02", "tmpV8": ".01", "tmpV9": ".53", "tmpV15": "X1", "tmpV16": ".1"}]))
    f = F.fee_rows(rows, {"X1"})["X1"]
    assert f["mgmt"] == 0.3 and f["total"] == 0.53 and f["other"] is None and f["ter"] == 0.63


# ── 지표 ──
def test_base_date_and_align_bm():
    idx = pd.to_datetime(["2017-12-29", "2020-12-30", "2021-01-29", "2021-02-01", "2021-03-01"])
    end = idx[-1]
    assert F.base_date(idx, end, "1M") == pd.Timestamp("2021-02-01")
    assert F.base_date(idx, end, "YTD") == pd.Timestamp("2021-01-29")      # 올해 첫 기준가
    assert F.base_date(idx[2:], end, "YTD") is None                     # 전년 이력이 없으면 없음
    assert F.base_date(idx, end, "3Y") == pd.Timestamp("2017-12-29")
    assert F.base_date(idx[1:], end, "1Y") is None
    bm = pd.Series([1.0, 2.0, 3.0, 4.0], index=pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06"]))
    d = pd.to_datetime(["2020-01-03", "2020-01-07"])
    assert list(F.align_bm(d, bm, 0)) == [3.0, 4.0] and list(F.align_bm(d, bm, 1)) == [2.0, 4.0] and list(F.align_bm(d, bm, 2)) == [1.0, 3.0]


def test_mid_pct_rank():
    pct, rank = F.mid_pct({"a": 10.0, "b": 20.0, "c": 20.0, "d": None, "e": -5.0})
    assert pct == {"a": 37.5, "b": 75.0, "c": 75.0, "e": 12.5} and rank == {"a": 3, "b": 1, "c": 1, "e": 4}
    assert F.mid_pct({"x": 1.0})[0] == {"x": 50.0}


def test_class_flows():
    end = {"A": {"units": 120.0, "nav": 1100.0}, "B": {"units": 50.0, "nav": 1000.0}}
    base = {"A": {"units": 100.0, "nav": 1000.0}}
    f = F.class_flows(["A", "B", "Z"], end, base)
    assert f["flow_eok"] == round((20 * 1100 + 50 * 1000) / 1e5, 1) and f["units_chg_pct"] == 70.0
    assert f["aum_chg_eok"] == round((120 * 1100 + 50 * 1000 - 100 * 1000) / 1e5, 1)
    assert F.class_flows(["A"], end, {})["flow_eok"] is None


def test_fund_metrics_returns_excess_and_risk():
    idx = pd.bdate_range("2020-01-01", "2021-06-30")
    nav = pd.Series(np.linspace(1000, 1500, len(idx)), index=idx)
    nav.iloc[-30] = nav.iloc[-31] * 0.8
    bm = pd.Series(np.linspace(100, 120, len(idx)), index=idx)
    rows = [{"date": d.strftime("%Y-%m-%d"), "nav": v, "adj_nav": v, "units_mn": 1.0, "aum_eok": 0} for d, v in nav.items()]
    m = F.fund_metrics(rows, bm, lag=0)
    end, d1m = idx[-1], F.base_date(idx, idx[-1], "1M")
    assert m["returns"]["1M"] == round((nav[end] / nav[d1m] - 1) * 100, 2)
    assert m["excess"]["1M"] == round(m["returns"]["1M"] - m["bm_returns"]["1M"], 2)
    assert m["returns"]["3Y"] is None and m["risk"]["mdd_1y"] == -20.0 and m["risk"]["vol_1y"] > 0
    m2 = F.fund_metrics(rows, None)
    assert m2["bm_returns"]["1M"] is None and m2["excess"]["1M"] is None


def test_bm_series_fx(tmp_path):
    (tmp_path / "e.csv").write_text("date,IDX,KRW=X\n2020-01-01,100,1000\n2020-01-02,110,\n2020-01-03,,1100\n", encoding="utf-8")
    assert list(F.bm_series({"file": "e.csv", "col": "IDX"}, hist_dir=tmp_path)) == [100.0, 110.0]
    assert list(F.bm_series({"file": "e.csv", "col": "IDX"}, hist_dir=tmp_path, fx=True)) == [100000.0, 110000.0]


def test_week_grid_median_and_detail():
    end = pd.Timestamp("2021-06-30")   # 수요일 — 마지막 점은 기준일
    g = F.week_grid(end, years=1)
    assert g[-1] == end and all(d.weekday() == 4 for d in g[:-1]) and len(g) == 53
    s = pd.Series([100.0, 110.0], index=pd.to_datetime(["2020-07-01", "2021-01-04"]))
    v = F.on_grid(s, g)
    assert not np.isnan(v.iloc[0]) and v.loc["2021-01-08"] == 110.0
    a = pd.Series([100.0, 110.0, 121.0], index=g[:3])
    b = pd.Series([np.nan, 100.0, 90.0], index=g[:3])
    med = F.median_index([a, b])
    assert list(med.round(4)) == [100.0, 110.0, round(110 * (1 + (0.1 - 0.1) / 2), 4)]
    charts = {"dates": ["d1", "d2", "d3"], "funds": {"X": {"v": [None, 50.0, 60.0], "g": "G", "b": "B", "bn": "BM"}},
              "bm": {"B": [10.0, 20.0, 30.0]}, "median": {"G": [100.0, 200.0, 100.0]}}
    d = F.detail(charts, "X", {"name": "펀드X"})
    assert d["fund"] == [None, 100.0, 120.0] and d["bm"] == [None, 100.0, 150.0] and d["group_median"] == [None, 100.0, 50.0]
    assert d["name"] == "펀드X" and d["bm_name"] == "BM" and F.detail(charts, "Y") is None


# ── 조립(오프라인) ──
DAYS = pd.bdate_range("2019-01-01", "2020-03-31")


def _nav(code, d):   # 합성 기준가 — 코드마다 다른 기울기
    i = DAYS.get_loc(d)
    return round(1000 * (1 + (0.0004 if code.endswith("1") else 0.0002) * i), 2)


@pytest.fixture()
def offline(monkeypatch, tmp_path):
    """금투협 호출을 합성 응답으로, 출력·캐시·BM 이력을 임시 폴더로."""
    monkeypatch.setattr(F, "OUT", tmp_path / "funds.json")
    monkeypatch.setattr(F, "CHARTS", tmp_path / "charts.json")
    monkeypatch.setattr(F, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(F, "HIST_DIR", tmp_path)
    monkeypatch.setattr(F, "DELAY", 0)
    monkeypatch.setattr(U, "OUT", tmp_path / "universe.json")
    lines = "".join(f"{d:%Y-%m-%d},{100 + i * 0.1},{100 + i * 0.05},{1000 + i}\n" for i, d in enumerate(DAYS))
    (tmp_path / "equity_daily.csv").write_text("date,^KS11,^KS200,KRW=X\n" + lines, encoding="utf-8")
    classes = {"K1": ("가나코리아증권자투자신탁(주식)ClassC-e", "2010-01-04"), "K2": ("가나코리아증권자투자신탁(주식)ClassA", "2010-01-04"),
               "K9": ("가나코리아증권자투자신탁(주식)(운용)", "2010-01-04"), "J2": ("다라밸류증권투자신탁(주식)C-e", "2012-01-02")}
    calls = []

    def fake_post(svc, cond, timeout=300, fn="select", dto="DISCondFuncDTO"):
        calls.append(svc)
        if svc == "DISFundStdPriceSO":
            d = pd.Timestamp(cond["tmpV30"])
            if cond["tmpV3"] != "02" or d not in DAYS:
                return _xml([]).replace("<L>", "<dbio_total_count_>0</dbio_total_count_><L>")
            return _xml([{"tmpV1": "가나운용", "tmpV2": n, "tmpV3": "주식형", "tmpV4": s.replace("-", ""), "tmpV5": "20000",
                          "tmpV6": str(_nav(c, d)), "tmpV12": c, "tmpV13": "M1"} for c, (n, s) in classes.items()])
        if svc == "DISFundStdPrcStutSO":
            ds = DAYS[(DAYS >= pd.Timestamp(cond["tmpV30"])) & (DAYS <= pd.Timestamp(cond["tmpV31"]))]
            return _xml([{"tmpV1": f"{d:%Y%m%d}", "tmpV2": str(_nav(cond["tmpV12"], d)), "tmpV5": "20000"} for d in ds])
        if svc == "DISFundRdmpSO":
            return _xml([{"tmpV4": "20191231", "tmpV7": "1200", "tmpV9": "1000", "tmpV11": "K1"}])
        if svc == "DISFundFeeCmsSO":
            return _xml([{"tmpV5": ".1", "tmpV9": ".2", "tmpV15": "K1"}]) if cond["tmpV30"] == "20200229" else "<root/>"
        if svc == "DISFundMngResResSO":
            ym, sub = (cond.split("<standardDt>")[1][:6], cond.split("<manageResBrnCd>")[1][:3])
            rows = {"111": [{"standardCd": c} for c in ("K1", "K2", "J2")]}.get(sub, [])
            return _xml([dict(r, fundNm="x") for r in rows], tag="DISFundMngResResDTO") if ym == "202002" else "<root/>"
        raise AssertionError(svc)
    monkeypatch.setattr(F, "_post", fake_post)
    return calls


def test_build_offline_then_incremental(offline, monkeypatch):
    d = F.build(today=dt.date(2020, 3, 31))
    m = d["meta"]
    assert m["as_of"] == "2020-03-31" and m["rop_month"] == "202002" and m["fee_as_of"] == "2020-02-29" and m["n_ok"] == 2
    by = {f["name"]: f for f in d["funds"]}
    k = by["가나코리아증권자투자신탁(주식)"]
    assert k["rep_code"] == "K1" and k["n_classes"] == 2 and k["aum_eok"] == pytest.approx(20000 * (_nav("K1", DAYS[-1]) + _nav("K2", DAYS[-1])) / 1e5, rel=1e-3)
    adj_end = _nav("K1", DAYS[-1]) * 1.2                   # 2019-12-31 분배(1200 → 1000) 뒤는 ×1.2
    d1m = F.base_date(DAYS, DAYS[-1], "1M")
    base = _nav("K1", d1m) * (1.2 if d1m > pd.Timestamp("2019-12-31") else 1)
    assert k["returns"]["1M"] == round((adj_end / base - 1) * 100, 2)
    assert k["group_id"] == "kr_active:일반" and by["다라밸류증권투자신탁(주식)"]["group_id"] == "kr_active:가치"
    assert k["pct"]["1Y"] == 50.0 and k["rank"]["1Y"] == 1 and k["n_group"] == 1 and k["bm"]["name"] == "KOSPI"
    assert k["fees"]["total"] == 0.2 and k["flows"]["flow_eok_1m"] == 0.0 and k["currency"] == "KRW"
    assert {g["id"] for g in d["groups"]} == {"kr_active:일반", "kr_active:가치"} and d["managers"][0]["n"] == 2
    charts = F.load_charts()
    assert charts["dates"][-1] == "2020-03-31" and set(charts["funds"]) == {"K1", "J2"} and "^KS11" in charts["bm"]
    det = F.detail(charts, "K1")
    assert det["fund"][next(i for i, x in enumerate(det["fund"]) if x is not None)] == 100.0
    assert json.loads(U.OUT.read_text(encoding="utf-8"))["n"] == 2
    # 다음 날: 펀드별 이력 호출 없이 일괄 스냅샷으로 이어 붙인다
    offline.clear()
    DAYS2 = DAYS.append(pd.DatetimeIndex(["2020-04-01"]))
    monkeypatch.setattr(sys.modules[__name__], "DAYS", DAYS2)
    d2 = F.build(today=dt.date(2020, 4, 1))
    assert d2["meta"]["as_of"] == "2020-04-01" and "DISFundStdPrcStutSO" not in offline
    assert {f["nav_date"] for f in d2["funds"]} == {"2020-04-01"}


def test_snapshot_caches_holiday_but_not_blocked_reply(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "CACHE", tmp_path)
    monkeypatch.setattr(F, "DELAY", 0)
    replies = {"ok": "<root><dbio_total_count_>0</dbio_total_count_></root>", "blocked": "<html>web-firewall</html>"}
    monkeypatch.setattr(F, "_post", lambda svc, cond, **k: replies[mode])
    mode = "blocked"
    assert F.snapshot(dt.date(2020, 1, 1), dt.date(2020, 2, 1)) == {} and not (tmp_path / "snap" / "20200101.json.gz").exists()
    mode = "ok"
    assert F.snapshot(dt.date(2020, 1, 1), dt.date(2020, 2, 1)) == {} and (tmp_path / "snap" / "20200101.json.gz").exists()


def test_run_skips_when_fresh(monkeypatch):
    called = []
    monkeypatch.setattr(F, "build", lambda *a, **k: called.append(1) or {"meta": {"n_ok": 1, "n_total": 1, "as_of": "x", "runtime_sec": 1}})
    monkeypatch.setattr(F, "age_hours", lambda: 2.0)
    refreshed = []
    monkeypatch.setattr(F, "refresh_bm", lambda *a, **k: refreshed.append(1) or "")
    assert F.run().startswith("건너뜀") and not called and not refreshed
    assert F.run(force=True).startswith("펀드 1/1") and called == [1] and refreshed == [1]   # 계산 전에 BM 갱신

    def boom(*a, **k):
        raise RuntimeError("Refinitiv 끊김")
    monkeypatch.setattr(F, "refresh_bm", boom)
    assert F.run(force=True).startswith("펀드 1/1")                                       # BM 갱신 실패해도 계산은 한다


def test_refresh_bm_only_fund_file_columns(monkeypatch, tmp_path):
    import refinitiv_src as R
    seen = {}

    def fake(path, tickers, start, **k):
        seen.update(path=path, tickers=tickers, start=start)
        return pd.DataFrame({t: [1.0] for t in tickers}, index=pd.DatetimeIndex(["2026-10-23"]))
    monkeypatch.setattr(R, "refresh_frame", fake)
    monkeypatch.setattr(F, "HIST_DIR", tmp_path)
    cfg = {"benchmarks": {"a": {"file": "equity_daily.csv", "col": "^GSPC"}, "b": {"file": F.BM_FILE, "col": ".X"},
                          "c": {"file": F.BM_FILE, "col": ".A"}, "d": None, "_note": "글"}}
    assert F.refresh_bm(cfg).startswith("BM .A, .X")
    assert seen["path"] == tmp_path / F.BM_FILE and seen["tickers"] == [".A", ".X", "KRW=X"]   # 환산용 KRW=X 도 같은 파일
    assert F.refresh_bm({"benchmarks": {"a": {"file": "equity_daily.csv", "col": "^GSPC"}}}).startswith("건너뜀")


def test_catalog_global_and_vietnam_bm_in_fund_file():
    import fund_universe as FU
    b = FU.load_cfg()["benchmarks"]
    for g in ("global:글로벌", "global:베트남"):
        assert b[g]["file"] == F.BM_FILE and b[g]["lag"] == 2 and b[g]["fx_unhedged"] is True
    assert b["global:테마·섹터"] is None and b["global:기타 지역"] is None


# ── API ──
LOCAL = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}, "headers": {"Host": "127.0.0.1:5101"}}
KEY = "k" * 20
DOC = {"generated_at": "2000-01-01T00:00:00", "meta": {"errors": ["X: [Errno 2] No such file: '/home/me/p/data/history/h.csv'"]},
       "method": "m", "groups": [], "managers": [], "flow_leaders": {}, "funds": [{"id": "X1", "name": "펀드", "ok": True}]}
CHARTS = {"dates": ["2000-01-07", "2000-01-14"], "funds": {"X1": {"v": [50.0, 55.0], "g": "G", "b": None, "bn": None}},
          "bm": {}, "median": {"G": [10.0, 11.0]}}


@pytest.fixture()
def saved(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "OUT", tmp_path / "funds.json")
    monkeypatch.setattr(F, "CHARTS", tmp_path / "charts.json")
    return tmp_path


def test_api_funds_default_saved_and_detail(saved):
    c = web.app.test_client()
    assert c.get("/api/funds", **LOCAL).get_json() == {"generated_at": None, "meta": {}, "method": None, "groups": [],
                                                       "managers": [], "flow_leaders": {}, "funds": []}
    assert c.get("/api/funds/X1", **LOCAL).status_code == 404
    F.OUT.write_text(json.dumps(DOC), encoding="utf-8")
    F.CHARTS.write_text(json.dumps(CHARTS), encoding="utf-8")
    assert c.get("/api/funds", **LOCAL).get_json() == DOC
    r = c.get("/api/funds/X1", **LOCAL).get_json()
    assert r["fund"] == [100.0, 110.0] and r["group_median"] == [100.0, 110.0] and r["bm"] is None and r["name"] == "펀드"
    assert c.get("/api/funds/NOPE", **LOCAL).status_code == 404
    for p in ("/api/funds", "/api/funds/X1"):
        assert web._lan_get_ok(p) and not any(p.startswith(h) for h in config.LAN_HIDE)


def test_api_funds_lan_strips_paths(saved, monkeypatch):
    F.OUT.write_text(json.dumps(DOC), encoding="utf-8")
    monkeypatch.setattr(config, "LAN_SHARE", True)
    monkeypatch.setattr(config, "LAN_ALLOW", ["10.0.0.0/24"])
    monkeypatch.setitem(web.LAN, "key", KEY)
    monkeypatch.setattr(web, "ALLOWED_HOSTS", web.ALLOWED_HOSTS | {"10.0.0.5:5101"})
    c = web.app.test_client()
    c.set_cookie("rc_key", KEY, domain="10.0.0.5")
    r = c.get("/api/funds", environ_base={"REMOTE_ADDR": "10.0.0.9"}, headers={"Host": "10.0.0.5:5101"})
    assert r.status_code == 200 and r.get_json()["meta"]["errors"] == ["X: [Errno 2] No such file: 'h.csv'"]


def test_share_snapshot_collects_funds_and_charts(monkeypatch, tmp_path):
    """공유본은 목록 + 차트 문서 하나를 담는다(펀드별 상세는 SHIM 이 차트 문서로 만든다)."""
    import share_site as S
    got = []

    def fake_get(path):
        got.append(path)
        if path == "/api/backtest/catalog":
            return {"presets": [{"value": k, "start": None, "end": None} for k in S.PRESETS], "strategies": []}
        return {}
    monkeypatch.setattr(S, "get", fake_get)
    monkeypatch.setattr(S, "post", lambda path, body: {})
    monkeypatch.setattr(S, "ROOT", tmp_path)
    snap, _ = S.collect()
    assert "/api/funds" in snap["get"] and "/api/funds/charts" in snap["get"]
    assert "fundDetail" in S.SHIM and "/api/funds/" in S.SHIM


def test_api_fund_charts(saved):
    c = web.app.test_client()
    assert c.get("/api/funds/charts", **LOCAL).get_json()["funds"] == {}
    F.CHARTS.write_text(json.dumps(CHARTS), encoding="utf-8")
    assert c.get("/api/funds/charts", **LOCAL).get_json() == CHARTS and web._lan_get_ok("/api/funds/charts")
