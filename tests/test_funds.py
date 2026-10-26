# -*- coding: utf-8 -*-
"""위탁운용사(funds)·증시자금(kofia_free) 회귀 테스트 — 합성 데이터만, 금투협·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import funds as F  # noqa: E402
import kofia_free as K  # noqa: E402
import web  # noqa: E402


def _xml(rows):
    metas = "".join("<selectMeta><vGridBtn1 />" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + "<tmpV11 /></selectMeta>"
                    for r in rows)
    return f'<?xml version="1.0"?><root><message><DISCondFuncListDTO>{metas}</DISCondFuncListDTO></message></root>'


# ── 금투협 XML 파싱 ──
def test_parse_and_nav_rows_skip_empty_and_unescape():
    text = _xml([{"tmpV1": "20200103", "tmpV2": "1010.5", "tmpV5": "300", "tmpV12": "X1"},
                 {"tmpV1": "20200102", "tmpV2": "1000", "tmpV5": "", "tmpV12": "X1"},
                 {"tmpV1": "20200101", "tmpV2": "0", "tmpV5": "1", "tmpV12": "X1"},
                 {"tmpV2": "A&amp;B"}])
    rows = F.parse(text)
    assert len(rows) == 4 and rows[3]["tmpV2"] == "A&B"
    nav = F.nav_rows(rows)
    assert list(nav) == ["2020-01-03", "2020-01-02"]          # 기준가 0 · 날짜 없는 행은 뺀다
    assert nav["2020-01-02"]["units_mn"] == 0.0 and nav["2020-01-03"] == {"date": "2020-01-03", "nav": 1010.5, "units_mn": 300.0}


def test_settle_rows_keeps_only_target_codes():
    rows = F.parse(_xml([{"tmpV4": "20200630", "tmpV7": "1200", "tmpV9": "1000", "tmpV10": "결산", "tmpV11": "X1"},
                         {"tmpV4": "20200630", "tmpV7": "1500", "tmpV9": "1000", "tmpV10": "결산", "tmpV11": "OTHER"}]))
    assert F.settle_rows(rows, {"X1"}) == [{"code": "X1", "end": "2020-06-30", "kind": "결산", "before": 1200.0, "after": 1000.0}]


def test_fee_rows_maps_fields_and_ter():
    rows = F.parse(_xml([{"tmpV1": "가나운용", "tmpV2": "가나펀드 C-e", "tmpV4": "20100101", "tmpV5": ".3", "tmpV6": ".2",
                          "tmpV7": ".02", "tmpV8": ".01", "tmpV9": ".53", "tmpV15": "X1", "tmpV16": ".1"}]))
    f = F.fee_rows(rows, {"X1"})["X1"]
    assert f["mgmt"] == 0.3 and f["total"] == 0.53 and f["other"] is None and f["ter"] == 0.63
    assert f["manager"] == "가나운용" and f["setup"] == "2010-01-01"
    assert F.fee_rows(rows, {"X2"}) == {}


# ── 수정기준가 ──
def test_adjust_applies_factor_after_settlement_end():
    hist = [{"date": "2020-06-29", "nav": 1190.0, "units_mn": 100.0}, {"date": "2020-06-30", "nav": 1200.0, "units_mn": 100.0},
            {"date": "2020-07-01", "nav": 1000.0, "units_mn": 120.0}, {"date": "2020-07-02", "nav": 1010.0, "units_mn": 120.0}]
    out = F.adjust(hist, [("2020-06-30", 1200.0, 1000.0)])
    assert [round(r["adj_nav"], 2) for r in out] == [1190.0, 1200.0, 1200.0, 1212.0]   # 결산 다음 날부터 ×1.2 — 이어진다
    assert out[2]["aum_eok"] == pytest.approx(120 * 1000 / 1e5)


# ── 지표 ──
def _rows(nav: pd.Series, units=100.0):
    return [{"date": d.strftime("%Y-%m-%d"), "nav": float(v), "adj_nav": float(v), "units_mn": units, "aum_eok": units * v / 1e5}
            for d, v in nav.items()]


def test_base_date_is_last_date_on_or_before_target():
    idx = pd.to_datetime(["2020-12-30", "2021-01-04", "2021-01-29", "2021-02-01", "2021-03-01"])
    end = idx[-1]
    assert F.base_date(idx, end, "1M") == pd.Timestamp("2021-02-01")
    assert F.base_date(idx, end, "YTD") == pd.Timestamp("2020-12-30")
    assert F.base_date(idx, end, "1Y") is None


def test_align_bm_lags():
    bm = pd.Series([1.0, 2.0, 3.0, 4.0], index=pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06"]))
    d = pd.to_datetime(["2020-01-03", "2020-01-07"])
    assert list(F.align_bm(d, bm, 0)) == [3.0, 4.0]
    assert list(F.align_bm(d, bm, 1)) == [2.0, 4.0]
    assert list(F.align_bm(d, bm, 2)) == [1.0, 3.0]
    assert np.isnan(F.align_bm(pd.to_datetime(["2020-01-01"]), bm, 1).iloc[0])


def test_fund_metrics_returns_excess_flows_and_risk():
    idx = pd.bdate_range("2020-01-01", "2021-06-30")
    nav = pd.Series(np.linspace(1000, 1500, len(idx)), index=idx)
    nav.iloc[-30] = nav.iloc[-31] * 0.8                    # 하루 −20% 낙폭(다음 날 회복)
    bm = pd.Series(np.linspace(100, 120, len(idx)), index=idx)
    rows = _rows(nav)
    rows[-1]["units_mn"] = 150.0                           # 마지막 날 설정원본 +50%
    m = F.fund_metrics(rows, bm, lag=0)
    end = idx[-1]
    d1m = F.base_date(nav.index, end, "1M")
    assert m["returns"]["1M"] == round((nav[end] / nav[d1m] - 1) * 100, 2)
    assert m["bm_returns"]["1M"] == round((bm[end] / bm[d1m] - 1) * 100, 2)
    assert m["excess"]["1M"] == round(m["returns"]["1M"] - m["bm_returns"]["1M"], 2)
    assert m["returns"]["YTD"] == round((nav[end] / nav[pd.Timestamp("2020-12-31")] - 1) * 100, 2)
    assert m["flows"]["units_chg_1m_pct"] == 50.0 and m["flows"]["units_chg_1m_eok"] == 0.5
    assert m["risk"]["mdd_1y"] == -20.0 and m["risk"]["mdd_all_from"] == "2020-01-01"
    assert m["risk"]["vol_1y"] > m["risk"]["bm_vol_1y"] > 0
    assert m["nav_date"] == "2021-06-30" and m["bm_date"] == "2021-06-30"
    c = m["chart"]
    assert c["fund"][0] == 100.0 and c["bm"][0] == 100.0 and c["dates"][-1] == "2021-06-30"
    assert len(c["dates"]) == len(set(c["dates"])) and len(c["dates"]) < len(idx) / 4


def test_fund_metrics_without_bm_and_short_history():
    idx = pd.bdate_range("2021-06-01", periods=10)
    m = F.fund_metrics(_rows(pd.Series(np.arange(10.0) + 100, index=idx)), None)
    assert m["returns"]["1W"] is not None and m["returns"]["1Y"] is None
    assert m["bm_returns"]["1W"] is None and m["excess"]["1W"] is None and m["bm_date"] is None
    assert m["risk"]["vol_1y"] is None                     # 20개 미만


def test_bm_series_yield_accrual(tmp_path):
    (tmp_path / "r.csv").write_text("date,cd\n2020-01-01,3.65\n2020-01-11,3.65\n2020-01-12,\n2020-01-21,0\n", encoding="utf-8")
    s = F.bm_series({"file": "r.csv", "col": "cd", "kind": "yield_accrual"}, hist_dir=tmp_path)
    assert list(s.index.strftime("%Y-%m-%d")) == ["2020-01-01", "2020-01-11", "2020-01-21"]
    assert s.iloc[0] == 100 and s.iloc[1] == pytest.approx(100.1) and s.iloc[2] == pytest.approx(100.1 * 1.001)


def test_catalog_is_valid():
    cat = F.load_catalog()
    assert 4 <= len(cat) <= 6 and len({f["code"] for f in cat}) == len(cat)
    for f in cat:
        assert f["bm"]["kind"] in ("price", "yield_accrual") and f["bm"]["file"].endswith(".csv") and f["type"] and f["manager"]


# ── 조립·건너뜀 ──
@pytest.fixture()
def offline(monkeypatch, tmp_path):
    """네트워크 호출을 합성 응답으로 바꾸고 출력·캐시를 임시 폴더로."""
    monkeypatch.setattr(F, "OUT", tmp_path / "funds.json")
    monkeypatch.setattr(F, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(F, "HIST_START", dt.date(2020, 1, 1))
    (tmp_path / "h.csv").write_text("date,BM\n" + "".join(f"{d:%Y-%m-%d},{100 + i}\n" for i, d in enumerate(pd.bdate_range("2020-01-01", "2020-03-31"))),
                                    encoding="utf-8")
    monkeypatch.setattr(F, "HIST_DIR", tmp_path)
    cat = tmp_path / "cat.json"
    cat.write_text(json.dumps({"funds": [
        {"code": "X1", "name": "합성1", "manager": "m", "type": "t", "note": "", "bm": {"name": "B", "file": "h.csv", "col": "BM", "kind": "price", "lag": 1}},
        {"code": "BAD", "name": "합성2", "manager": "m", "type": "t", "note": "", "bm": {"name": "B", "file": "h.csv", "col": "BM", "kind": "price", "lag": 1}}]}),
        encoding="utf-8")
    monkeypatch.setattr(F, "CATALOG", cat)
    calls = []

    def fake_post(svc, cond, timeout=300):
        calls.append((svc, cond))
        if svc == "DISFundStdPrcStutSO":
            if cond["tmpV12"] == "BAD":
                return _xml([])
            days = pd.bdate_range(max(pd.Timestamp(cond["tmpV30"]), pd.Timestamp("2020-01-01")), pd.Timestamp(cond["tmpV31"]))
            return _xml([{"tmpV1": f"{d:%Y%m%d}", "tmpV2": str(1000 + i), "tmpV5": "500", "tmpV12": cond["tmpV12"]} for i, d in enumerate(days)])
        if svc == "DISFundRdmpSO":
            return _xml([{"tmpV4": "20200131", "tmpV7": "1100", "tmpV9": "1000", "tmpV10": "결산", "tmpV11": "X1"}])
        if svc == "DISFundFeeCmsSO":
            return _xml([{"tmpV1": "m", "tmpV2": "합성1", "tmpV4": "20100101", "tmpV5": ".1", "tmpV9": ".2", "tmpV15": "X1"}]) if cond["tmpV30"] == "20200229" else "<root/>"
        raise AssertionError(svc)
    monkeypatch.setattr(F, "_post", fake_post)
    return calls


def test_build_offline_one_fund_fails_others_ok(offline, tmp_path):
    d = F.build(today=dt.date(2020, 3, 31))
    x1, bad = d["funds"]
    assert x1["ok"] and not bad["ok"]
    assert d["meta"]["n_ok"] == 1 and d["meta"]["fee_as_of"] == "2020-02-29" and x1["fees"]["total"] == 0.2
    assert d["meta"]["sources"] == [F.SOURCE] and any("BAD" in e for e in d["meta"]["errors"])
    assert x1["settlements"] == [{"end": "2020-01-31", "factor": 1.1}]
    assert x1["bm_returns"]["1M"] is not None and len(d["meta"]["errors"]) == 1
    assert not any(str(tmp_path) in e for e in d["meta"]["errors"])
    assert json.loads(F.OUT.read_text(encoding="utf-8"))["funds"][0]["code"] == "X1"


def test_history_incremental_refetches_only_recent(offline):
    F.history("X1", dt.date(2020, 1, 1), dt.date(2020, 2, 28))
    offline.clear()
    rows = F.history("X1", dt.date(2020, 1, 1), dt.date(2020, 3, 31))
    (svc, cond), = offline
    assert cond["tmpV30"] == "20200218" and rows[0]["date"] == "2020-01-01" and rows[-1]["date"] == "2020-03-31"


def test_run_skips_when_fresh(offline, monkeypatch):
    called = []
    monkeypatch.setattr(F, "build", lambda *a, **k: called.append(1) or {"meta": {"n_ok": 1, "n_total": 1, "as_of": "x"}})
    monkeypatch.setattr(F, "age_hours", lambda: 2.0)
    assert F.run().startswith("건너뜀") and not called
    assert F.run(force=True).startswith("펀드 1/1") and called == [1]
    monkeypatch.setattr(F, "age_hours", lambda: None)
    F.run()
    assert called == [1, 1]


# ── API ──
LOCAL = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}, "headers": {"Host": "127.0.0.1:5101"}}
KEY = "k" * 20
DOC = {"generated_at": "2000-01-01T00:00:00", "meta": {"errors": ["X: [Errno 2] No such file: '/home/me/p/data/history/h.csv'"]},
       "method": "m", "funds": [{"code": "X1", "ok": True}]}


def test_api_funds_default_and_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "OUT", tmp_path / "funds.json")
    c = web.app.test_client()
    assert c.get("/api/funds", **LOCAL).get_json() == {"generated_at": None, "meta": {}, "method": None, "funds": []}
    F.OUT.write_text(json.dumps(DOC), encoding="utf-8")
    assert c.get("/api/funds", **LOCAL).get_json() == DOC
    assert "/api/funds" in config.LAN_GET_ALLOW and web._lan_get_ok("/api/funds")
    assert not any("/api/funds".startswith(h) for h in config.LAN_HIDE)


def test_api_funds_lan_strips_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "OUT", tmp_path / "funds.json")
    F.OUT.write_text(json.dumps(DOC), encoding="utf-8")
    monkeypatch.setattr(config, "LAN_SHARE", True)
    monkeypatch.setattr(config, "LAN_ALLOW", ["10.0.0.0/24"])
    monkeypatch.setitem(web.LAN, "key", KEY)
    monkeypatch.setattr(web, "ALLOWED_HOSTS", web.ALLOWED_HOSTS | {"10.0.0.5:5101"})
    c = web.app.test_client()
    c.set_cookie("rc_key", KEY, domain="10.0.0.5")
    r = c.get("/api/funds", environ_base={"REMOTE_ADDR": "10.0.0.9"}, headers={"Host": "10.0.0.5:5101"})
    assert r.status_code == 200 and r.get_json()["meta"]["errors"] == ["X: [Errno 2] No such file: 'h.csv'"]


def test_share_snapshot_collects_funds(monkeypatch, tmp_path):
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
    assert "/api/funds" in got and "/api/funds" in snap["get"]


# ── FreeSIS 증시자금 ──
def _ds(rows):
    return [{"TMPV1": d, **{f"TMPV{i + 2}": v for i, v in enumerate(vals)}} for d, vals in rows]


def test_kofia_free_parse_and_combine():
    dep = K.parse(_ds([("19980619", [11, 2, 3, 4, 5, 0.5]), ("19980618", [10, 2, 3, 4, 5, 0.4]), ("", [1] * 6)]),
                  K.SERVICES["STATSCU0100000060BO"])
    cr = K.parse(_ds([("19980619", [0] * 8), ("19980701", [7, 5, 2, 1, 1, 0, 0, 3]), ("19980130", [0] * 8)]),
                 K.SERVICES["STATSCU0100000070BO"])
    assert list(dep.index.strftime("%Y%m%d")) == ["19980618", "19980619"] and dep["customer_deposits"].iloc[-1] == 11
    df = K.combine([dep, cr])
    assert list(df.columns) == K.COLUMNS
    assert list(df.index.strftime("%Y%m%d")) == ["19980618", "19980619", "19980701"]   # 전부 빈 1998-01-30 은 뺀다
    assert df.loc["1998-06-19", K.CREDIT_COLS].isna().all()                              # 유효 시작 전 0 → 빈 값
    assert df.loc["1998-07-01", "credit_balance"] == 7 and pd.isna(df.loc["1998-07-01", "customer_deposits"])


def test_kofia_free_run_incremental(monkeypatch, tmp_path):
    monkeypatch.setattr(K, "OUT", tmp_path / "k.csv")
    calls = []

    def fake_fetch(obj, start, end):
        calls.append((obj, start))
        days = pd.bdate_range(start, end)
        n = len(K.SERVICES[obj])
        bump = 1000 if len(calls) > 2 else 0               # 두 번째 실행은 정정된 값
        return [{"TMPV1": f"{d:%Y%m%d}", **{f"TMPV{i + 2}": 100 + bump for i in range(n)}} for d in reversed(days)]
    monkeypatch.setattr(K, "_fetch", fake_fetch)
    K.run(today=dt.date(1998, 8, 31))
    assert calls[0][1] == K.START
    K.run(today=dt.date(1998, 9, 15))
    assert calls[-1][1] == dt.date(1998, 8, 21)            # 마지막 날 − 10일부터
    df = K.load()
    assert df.index.max() == pd.Timestamp("1998-09-15") and df.loc["1998-08-20", "customer_deposits"] == 100
    assert df.loc["1998-08-21", "customer_deposits"] == 1100 and not df.index.duplicated().any()
