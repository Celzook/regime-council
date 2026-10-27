# -*- coding: utf-8 -*-
"""페어 후보·DART 지분 관계(pair_universe·dart_src) 회귀 테스트 — 합성 데이터, Refinitiv·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dart_src as D  # noqa: E402
import pair_universe as U  # noqa: E402


def _master(rows):
    cols = ["ric", "code", "name", "kind", "market", "gics_code", "gics_name", "mktcap", "org_id", "idx_proxy"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows]).set_index("ric")


M = _master([
    ("005930.KS", "005930", "삼성전자", "보통", "KOSPI", "452020", "HW", 100e12, "O1", True),
    ("005935.KS", "005935", "삼성전자우", "우선", "KOSPI", "452020", "HW", 100e12, "O1", False),
    ("009150.KS", "009150", "삼성전기", "보통", "KOSPI", "452020", "HW", 10e12, "O2", True),
    ("028260.KS", "028260", "삼성물산", "보통", "KOSPI", "201050", "Conglo", 30e12, "O3", True),
    ("000270.KS", "000270", "기아", "보통", "KOSPI", "251020", "Auto", 40e12, "O4", True),
    ("005380.KS", "005380", "현대차", "보통", "KOSPI", "251020", "Auto", 60e12, "O5", True),
    ("005385.KS", "005385", "현대차우", "우선", "KOSPI", "251020", "Auto", 60e12, "O5", False),
    ("005387.KS", "005387", "현대차2우B", "우선", "KOSPI", "251020", "Auto", 60e12, "O5", False),
    ("123456.KQ", "123456", "외톨이", "보통", "KOSDAQ", None, None, 1e10, "O6", False),
    ("999995.KS", "999995", "주인없는우", "우선", "KOSPI", "251020", "Auto", 1e10, "O9", False),
])


# ── 이름 정규화 ──
@pytest.mark.parametrize("raw,norm", [("(주)한화", "한화"), ("㈜ 한화", "한화"), ("주식회사 LG", "lg"), ("SK hynix Inc.", "skhynix"),
                                      ("현대자동차(주)", "현대자동차"), ("Incross Co., Ltd.", "incross"), ("Hyundai Corporation", "hyundai")])
def test_norm(raw, norm):
    assert D._norm(raw) == norm


def test_report_year_switches_on_april_first():
    import datetime as dt
    assert D.report_year(dt.date(2026, 3, 31)) == 2024
    assert D.report_year(dt.date(2026, 4, 1)) == 2025


# ── 후보 ──
def _cands(links=None, top_n=10, monkeypatch=None):
    return {c["id"]: c for c in U.candidates(M, {"links": links or []}, top_n=top_n)}


def test_pref_common_pairs_each_pref_with_its_common():
    c = _cands()
    assert c["005935.KS~005930.KS"]["kind"] == "보통·우선"
    assert c["005385.KS~005380.KS"]["kind"] == "보통·우선" and c["005387.KS~005380.KS"]["kind"] == "보통·우선"
    assert not any(k.startswith("999995.KS") for k in c)     # 같은 법인 보통주가 없으면 후보 아님


def test_industry_pairs_only_ordinary_and_top_n():
    c = _cands(top_n=10)
    assert c["000270.KS~005380.KS"]["kind"] == "동종업종"
    assert not any("123456.KQ" in k for k in c)              # GICS 없는 종목은 동종업종 후보 아님
    assert all("우" not in M.loc[x, "name"] for v in c.values() if v["kind"] == "동종업종" for x in (v["a"], v["b"]))
    c1 = _cands(top_n=1)
    assert not any(v["kind"] == "동종업종" for v in c1.values())   # 업종마다 1종목이면 조합 없음


def test_overlap_keeps_priority_kind_and_notes_other():
    # 기아(자) ← 현대차(모) 35% 는 모자회사이면서 같은 업종 — 모자회사가 남고 note 에 동종업종
    c = _cands(links=[{"parent": "005380", "child": "000270", "pct": 35.17, "as_of": "2025-12-31",
                       "parent_name": "현대자동차", "child_name": "기아"}])
    v = c.get("000270.KS~005380.KS") or c.get("005380.KS~000270.KS")
    assert v["kind"] == "모자회사" and v["a"] == "000270.KS" and v["b"] == "005380.KS"
    assert v["pct"] == 35.17 and "동종업종" in v["note"]
    assert sum(1 for k in c if set(k.split("~")) == {"000270.KS", "005380.KS"}) == 1


def test_exclude_override_drops_pair_either_order(monkeypatch):
    monkeypatch.setattr(D, "load_overrides", lambda: {"name_to_code": {}, "exclude": ["005930.KS~005935.KS"]})
    c = {x["id"] for x in U.candidates(M, {"links": []})}
    assert "005935.KS~005930.KS" not in c


# ── DART 관계 ──
def test_links_matches_names_threshold_and_dedups(monkeypatch, tmp_path):
    listed = {"005380": {"corp_code": "C1", "name": "현대자동차"}, "000270": {"corp_code": "C2", "name": "기아"},
              "004020": {"corp_code": "C3", "name": "현대제철"}, "012330": {"corp_code": "C4", "name": "현대모비스"}}
    rows = {"C1": [{"inv_prm": "기아(주)", "pct": 35.17, "purpose": "경영참여", "stlm_dt": "2025-12-31", "rcept_no": "r1"},
                   {"inv_prm": "현대제철", "pct": 6.87, "purpose": "경영참여", "stlm_dt": "2025-12-31", "rcept_no": "r1"},
                   {"inv_prm": "비상장자회사", "pct": 100.0, "purpose": "경영참여", "stlm_dt": "2025-12-31", "rcept_no": "r1"},
                   {"inv_prm": "기아", "pct": 20.0, "purpose": "경영참여", "stlm_dt": "2025-12-31", "rcept_no": "r1"}],
            "C4": [{"inv_prm": "모비스별칭", "pct": 21.0, "purpose": "경영참여", "stlm_dt": "2025-12-31", "rcept_no": "r4"}]}
    monkeypatch.setattr(D, "RAW", tmp_path)
    monkeypatch.setattr(D, "corp_codes", lambda refresh=False: listed)
    monkeypatch.setattr(D, "load_overrides", lambda: {"name_to_code": {"모비스별칭": "005380"}, "exclude": []})
    monkeypatch.setattr(D, "DELAY", 0)
    calls = []

    def fake(cc, year, cache=None):
        calls.append((cc, year))
        r = rows.get(cc, []) if year == D.report_year() else []
        if cache is not None:
            cache[cc] = r
        return r
    monkeypatch.setattr(D, "investments", fake)
    d = D.links(min_pct=7.0)
    got = {(l["parent"], l["child"]): l["pct"] for l in d["links"]}
    assert got == {("005380", "000270"): 35.17, ("012330", "005380"): 21.0}   # 7% 미만 현대제철 제외, 기아 두 줄 → 큰 값
    assert {"parent": "005380", "inv_prm": "비상장자회사", "pct": 100.0} in d["unmatched"]
    # codes 로 좁히면 그 밖 종목이 낀 관계는 빠진다
    d2 = D.links(codes={"005380", "000270"}, min_pct=7.0)
    assert {(l["parent"], l["child"]) for l in d2["links"]} == {("005380", "000270")}


def test_investments_failure_not_cached(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(D, "_get", boom)
    cache = {}
    assert D.investments("C1", 2025, cache) is None and "C1" not in cache


# ── 가격 병합 ──
def test_merge_overwrites_overlap_and_keeps_old_columns():
    idx = pd.bdate_range("2026-01-05", periods=4)
    old = pd.DataFrame({"A": [1.0, 2.0, 3.0, None], "B": [9.0, 9.0, 9.0, 9.0]}, index=idx)
    new = pd.DataFrame({"A": [30.0, 4.0], "C": [5.0, 6.0]}, index=idx[2:])
    m = U._merge(old, new)
    assert list(m["A"]) == [1.0, 2.0, 30.0, 4.0]     # 겹친 날은 새 값(정정)
    assert list(m["B"]) == [9.0] * 4                 # 이번에 안 받은 열은 그대로
    assert m["C"].isna().sum() == 2 and list(m["C"].dropna()) == [5.0, 6.0]


# ── 주식 수 확인(이름 충돌 거르기) ──
def test_verify_shares_drops_name_collisions():
    m = _master([("000270.KS", "000270", "기아", "보통", "KOSPI", "251020", "Auto", 40e12, "O4", True),
                 ("352820.KS", "352820", "하이브", "보통", "KOSPI", "502010", "Ent", 10e12, "O7", True),
                 ("111111.KS", "111111", "주식수없음", "보통", "KOSPI", "502010", "Ent", 1e12, "O8", False)])
    m["shares"] = [388_656_043, 41_651_000, None]
    links = [{"parent": "005380", "child": "000270", "pct": 35.17, "qy": 137_318_251},     # 35.3% — 맞음
             {"parent": "000000", "child": "352820", "pct": 100.0, "qy": 2_000_000},        # 4.8% ≠ 100% — 이름만 같은 비상장
             {"parent": "000000", "child": "111111", "pct": 20.0, "qy": 1_000},
             {"parent": "000001", "child": "000270", "pct": 10.0, "qy": None},
             {"parent": "000002", "child": "000270", "pct": 6.0, "qy": 233_193_626}]                # ×10 — 결산일 뒤 병합으로 보고 남김
    keep, drop = U.verify_shares(links, m)
    assert [(l["parent"], l["implied_pct"]) for l in keep] == [("005380", 35.33), ("000002", 60.0)]
    assert "share_note" not in keep[0] and "병합" in keep[1]["share_note"] and keep[1]["pct"] == 6.0   # 병합은 공시 지분율 그대로
    assert {d["child"] for d in drop} == {"352820", "111111", "000270"}
    assert any("이름만 같은 다른 회사이거나" in d["reason"] for d in drop if d["child"] == "352820")


def test_links_prefers_current_listed_code_on_name_collision(monkeypatch, tmp_path):
    # DART 목록에 같은 이름의 상장폐지 회사(종목코드 남음)가 있으면 현재 상장(codes 안) 쪽으로 잇는다
    listed = {"035420": {"corp_code": "P", "name": "NAVER"}, "006800": {"corp_code": "C", "name": "미래에셋증권"},
              "037620": {"corp_code": "X", "name": "미래에셋증권"}}                         # 옛 미래에셋증권(상장폐지)
    monkeypatch.setattr(D, "RAW", tmp_path)
    monkeypatch.setattr(D, "corp_codes", lambda refresh=False: listed)
    monkeypatch.setattr(D, "load_overrides", lambda: {"name_to_code": {}, "exclude": []})
    monkeypatch.setattr(D, "DELAY", 0)
    rows = {"P": [{"inv_prm": "미래에셋증권", "pct": 8.15, "qy": 1.0, "purpose": "", "stlm_dt": "", "rcept_no": ""}]}

    def fake(cc, year, cache=None):
        r = rows.get(cc, []) if year == D.report_year() else []
        if cache is not None:
            cache[cc] = r
        return r
    monkeypatch.setattr(D, "investments", fake)
    for order in (listed, dict(reversed(list(listed.items())))):
        monkeypatch.setattr(D, "corp_codes", lambda refresh=False, o=order: o)
        d = D.links(codes={"035420", "006800"}, min_pct=7.0)
        assert [(l["parent"], l["child"]) for l in d["links"]] == [("035420", "006800")]


def test_dart_errors_do_not_leak_key(monkeypatch):
    import requests
    monkeypatch.setattr(D.config, "api_key", lambda name: "SECRETKEY123")

    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError(f"failed {url}?crtfc_key={params['crtfc_key']}")
    monkeypatch.setattr(D.requests, "get", boom)
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    with pytest.raises(requests.RequestException) as e:
        D._get("company.json", {})
    assert "SECRETKEY123" not in str(e.value) and "***" in str(e.value)


def test_verify_shares_dilution_uses_lower_pct():
    m = _master([("000270.KS", "000270", "자회사", "보통", "KOSPI", "251020", "Auto", 40e12, "O4", True)])
    m["shares"] = [200_000_000]
    keep, _ = U.verify_shares([{"parent": "1", "child": "000270", "pct": 60.0, "qy": 60_000_000}], m)   # 30% — 상장 희석 ×0.5
    assert keep[0]["pct"] == 30.0 and keep[0]["dart_pct"] == 60.0 and "희석" in keep[0]["share_note"]


def test_load_prices_refills_columns_that_fell_behind(monkeypatch, tmp_path):
    import datetime as dt
    idx = pd.bdate_range(end=pd.Timestamp(dt.date.today()) - pd.Timedelta(days=1), periods=60)
    old = pd.DataFrame({"A.KS": 1.0, "B.KS": 1.0}, index=idx)
    old.loc[idx[-30]:, "B.KS"] = None                               # B 는 30영업일 전부터 비어 있음
    monkeypatch.setattr(U, "RET", tmp_path / "r.csv")
    monkeypatch.setattr(U, "TV", tmp_path / "t.csv")
    U._write_csv(old.copy(), U.RET)
    U._write_csv(old.copy(), U.TV)
    monkeypatch.setattr(U.R, "available", lambda force=False: True)
    calls = []

    def fake_fetch(group, since, today):
        calls.append((tuple(group), since))
        return pd.DataFrame(), pd.DataFrame()
    monkeypatch.setattr(U, "_fetch", fake_fetch)
    U.load_prices(["A.KS", "B.KS"])
    got = dict(calls)
    assert got[("A.KS",)] >= (idx[-1] - pd.Timedelta(days=U.REFETCH_DAYS)).date()
    assert got[("B.KS",)] <= (idx[-31]).date()                      # B 는 자기 마지막 값부터 다시


def test_empty_dart_result_is_retried_after_30_days(monkeypatch, tmp_path):
    import datetime as dt
    import json as js
    y = D.report_year()
    listed = {"000001": {"corp_code": "C1", "name": "가"}}
    old = (dt.date.today() - dt.timedelta(days=D.EMPTY_RETRY_DAYS + 1)).isoformat()
    (tmp_path / f"otr_{y}.json").write_text(js.dumps({"C1": [], D.EMPTY_KEY: {"C1": old}}), encoding="utf-8")
    (tmp_path / f"otr_{y - 1}.json").write_text(js.dumps({"C1": [], D.EMPTY_KEY: {"C1": dt.date.today().isoformat()}}), encoding="utf-8")
    monkeypatch.setattr(D, "RAW", tmp_path)
    monkeypatch.setattr(D, "corp_codes", lambda refresh=False: listed)
    monkeypatch.setattr(D, "load_overrides", lambda: {"name_to_code": {}, "exclude": []})
    monkeypatch.setattr(D, "DELAY", 0)
    calls = []

    def fake_get(path, params, timeout=30):
        calls.append(params["bsns_year"])

        class R:
            def json(self):
                return {"status": D.NO_DATA}
        return R()
    monkeypatch.setattr(D, "_get", fake_get)
    D.links(min_pct=7.0)
    assert calls == [str(y)]                       # 오래된 '자료 없음'만 다시, 최근 것(y−1)은 캐시
    saved = js.loads((tmp_path / f"otr_{y}.json").read_text(encoding="utf-8"))
    assert saved[D.EMPTY_KEY]["C1"] == dt.date.today().isoformat()
