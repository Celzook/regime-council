# -*- coding: utf-8 -*-
"""ELS 화면(els.py · /api/els) 회귀 테스트 — 합성 XML·합성 값만, 금투협·네트워크 없이 돈다.
실행: python -m pytest tests/test_els.py   (Windows 파이썬)
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import els as E  # noqa: E402
import refresh_all  # noqa: E402
import web  # noqa: E402

LOCAL = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}, "headers": {"Host": "127.0.0.1:5101"}}
KEY = "k" * 20


def _dto(**v) -> str:
    return "<DISDlsDTO>" + "".join(f"<{k}>{x}</{k}>" if x != "" else f"<{k} />" for k, x in v.items()) + "</DISDlsDTO>"


# 사이트 응답 모양 그대로 — 값은 HTML 이스케이프, 기초자산은 &lt;br/> 로 이음, 머리글 순서와 다른 val26(발행일)·val23(최대손실률)
ROW_A = dict(val1="", val2="A03044", val3="3", val4="키움증권", val5="AA", val6="키움증권 제4247회 파생결합증권(주가연계증권)", val7="3",
             val8="S&amp;P500 Index&lt;br/>Nikkei225 Index&lt;br/>Euro Stoxx 50 Index", val9="S&amp;P500 Index", val10="Nikkei225 Index",
             val11="Euro Stoxx 50 Index", val12="", val13="", val14="20291004", val15="10.4", val16="20260917", val17="20261001",
             val18="[스텝다운] 3년/6개월/85-85-85-80-75-70/KI45", val20="https://www.kiwoom.com/x?a=1&amp;b=2", val21="2026.10.01 13:00 청약종료",
             val22="KR6KW0006216", val23="-100", val26="20261002", fileNm="e4d3.pdf", serverPath="/fsfile/report/receipt",
             originalFileNm="간이투자설명서(ELS 4247).pdf")
ROW_B = dict(ROW_A, val4="교보증권", val6="교보증권(ELS) 2124", val7="2", val8="KOSPI200 Index&lt;br/>삼성전자", val9="KOSPI200 Index",
             val10="삼성전자", val11="", val18="스텝다운 노낙인 / 3년만기, 6개월마다 조기상환 기회부여 / 65-65-65-65-60-60, noKI  / 연 5.20%",
             val20="www.iprovest.com/p", val22="KR6ZY00058N4", val15="5.2", val26="20261005", fileNm="", serverPath="", originalFileNm="")


def _xml(*rows) -> str:
    echo = _dto(val1="", val6="")      # 입력 반향 블록(상품명 없음) — 행으로 치지 않는다
    return f'<?xml version="1.0"?><root><message><DISDlsListDTO>{echo}{"".join(_dto(**r) for r in rows)}</DISDlsListDTO></message></root>'


def test_parse_and_row_mapping():
    a, b = (E.row(r) for r in E.parse(_xml(ROW_A, ROW_B)))
    assert a["issue"] == "2026-10-02" and a["maturity"] == "2029-10-04"            # val26 발행일 · val14 만기일
    assert (a["sub_start"], a["sub_end"]) == ("2026-09-17", "2026-10-01")           # val16 · val17
    assert a["coupon"] == 10.4 and a["max_loss"] == -100 and a["code"] == "KR6KW0006216" and a["years"] == 3
    assert (a["issuer"], a["issuer_cd"], a["rating"]) == ("키움증권", "A03044", "AA")
    assert a["under"] == ["S&P500", "Nikkei225", "Euro Stoxx 50"] and a["stocks"] == [] and a["under_kind"] == "지수"
    assert a["url"] == "https://www.kiwoom.com/x?a=1&b=2" and a["note"] == "2026.10.01 13:00 청약종료"
    assert a["doc"].startswith("https://disdown.kofia.or.kr/COMFSFileDownload.jsp?serverPath=%2Ffsfile%2Freport%2Freceipt&serverFileNm=e4d3.pdf&filename=")
    assert (a["ki"], a["no_ki"], a["barriers"], a["first_barrier"], a["period_m"]) == (45, False, "85-85-85-80-75-70", 85, 6)
    assert b["under"] == ["KOSPI200", "삼성전자"] and b["stocks"] == ["삼성전자"] and b["under_kind"] == "혼합"
    assert b["url"] == "http://www.iprovest.com/p" and b["doc"] is None and b["no_ki"] and b["ki"] is None


@pytest.mark.parametrize("text,ki,no_ki,barriers,period", [
    ("[온라인 전용상품]기초자산:KOSPI200,HSCEI,S&P500/3년만기 6개월단위 조기상환형/StepDown형[85-80-80-75-75-75/50(종가)]/쿠폰:연12.00%", 50, False, "85-80-80-75-75-75", 6),
    ("[온라인 전용상품]원금비보장, 85-85-80-75-70-55/35 KI", 35, False, "85-85-80-75-70-55", None),
    ("3년 만기/ 상환주기 6개월--Lizard Step-Down형90(L75)/90(L70)/85/80/75/70--knock in 40", 40, False, "90-90-85-80-75-70", 6),
    ("[리자드] 3년/6개월,45%-(95,90,85,85,80,75)%,리자드 상환", 45, False, "95-90-85-85-80-75", 6),
    ("3Y/4M KI Stepdown ELS, 80-80-80-75-75-75-70-70-70 / KI35", 35, False, "80-80-80-75-75-75-70-70-70", 4),
    ("90-85-80-75-70-65, KI_35 (조기상환 스텝다운형)", 35, False, "90-85-80-75-70-65", None),
    ("스텝다운 낙인 / 3년만기, 4개월마다 조기상환 기회부여 / 85-80-80-80-75-75-75-70-70 KI 35 / 연 15.90%", 35, False, "85-80-80-80-75-75-75-70-70", 4),
    ("36개월 만기, 6개월마다 총 5회 조기상환 기회 부여 85-85-85-85-75-60/ KI 없음, CB 55%", None, True, "85-85-85-85-75-60", 6),
    ("월지급식Hi-Five ELS(90%원금지급형), 3년만기, 3개월마다 조기상환 기회부여, 85-85-85-85, NO KI, 월쿠폰베리어 85%", None, True, "85-85-85-85", 3),
    ("KOSPI200+EUROSTOXX50+S&P500, 3y/6m 85-85-80-80-75-65, Cpn=13.70%", None, False, "85-85-80-80-75-65", 6),   # 낙인 표시 없음 = 확인 불가
    ("만기 9개월, 4개월(1회) 조기상환 시 수익률 4.95%(연 14.80%)", None, False, None, 4),
    ("Tesla, 90(4m)-90(5m)-90(6m)-80, Cpn=9.00%", None, False, None, None),
    ("원금부분지급형, 만기3개월, 조건충족 시 예상수익률 지급", None, False, None, None),
])
def test_parse_structure(text, ki, no_ki, barriers, period):
    s = E.parse_structure(text)
    assert (s["ki"], s["no_ki"], s["barriers"], s["period_m"]) == (ki, no_ki, barriers, period)


def test_chunks_cover_window_and_future_issue():
    since, cs = E.chunks(dt.date(2026, 10, 29))
    assert since == dt.date(2026, 7, 29)
    assert [(c[0], c[1], c[2]) for c in cs] == [("202607", dt.date(2026, 7, 1), dt.date(2026, 7, 31)),
                                                ("202608", dt.date(2026, 8, 1), dt.date(2026, 8, 31)),
                                                ("202609", dt.date(2026, 9, 1), dt.date(2026, 9, 30)),
                                                ("202610", dt.date(2026, 10, 1), dt.date(2026, 11, 28))]   # 이번 달은 오늘 + 30일까지


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(E, "CACHE", tmp_path / "kofia")
    monkeypatch.setattr(E, "OUT", tmp_path / "els.json")
    monkeypatch.setattr(E, "DELAY", 0)

    def ended(start, end):
        calls.append(f"{start:%Y%m}")
        if start.month == 10:
            return E.parse(_xml(ROW_A, ROW_B))
        return E.parse(_xml(dict(ROW_A, val22=f"OLD{start.month}", val26=f"2026{start.month:02d}15")))
    monkeypatch.setattr(E, "fetch_ended", ended)
    monkeypatch.setattr(E, "fetch_subscribing", lambda: E.parse(_xml(dict(ROW_B, val6="청약중 상품", val22="KR6ZY00058N4"))))
    return calls


def test_build_dedups_filters_window_and_reuses_settled_months(fake):
    today = dt.date(2026, 10, 29)
    d = E.build(today=today)
    assert fake == ["202607", "202608", "202609", "202610"]
    assert [x["name"] for x in d["subscribing"]] == ["청약중 상품"]
    codes = [x["code"] for x in d["ended"]]
    assert "KR6ZY00058N4" not in codes                  # 청약중에도 있으면 청약중에만
    assert "OLD7" not in codes and {"OLD8", "OLD9", "KR6KW0006216"} <= set(codes)   # 7/15 발행은 창(7/29~) 밖
    assert d["meta"]["window"] == {"from": "2026-07-29", "to": "2026-10-29"} and json.loads(E.OUT.read_text(encoding="utf-8"))["meta"]["n_ended"] == 3
    fake.clear()
    E.build(today=today)
    assert fake == ["202610"]                           # 끝나고 10일 지난 뒤 받은 달은 다시 받지 않는다
    p = E.CACHE / "els_end_202609.json"                 # 달이 막 끝났을 때 받은 보관본은 정착 뒤 한 번 더 받는다
    c = json.loads(p.read_text(encoding="utf-8"))
    c["fetched_at"] = "2026-10-05T17:00:00"
    p.write_text(json.dumps(c), encoding="utf-8")
    fake.clear()
    m = E.build(today=today)["meta"]
    assert fake == ["202609", "202610"] and [c["cached"] for c in m["chunks"]] == [True, True, False, False]


def test_failed_month_falls_back_to_cache_and_empty_raises(fake, monkeypatch):
    today = dt.date(2026, 10, 29)
    E.build(today=today)

    def boom(start, end):
        raise OSError("연결 끊김 /home/me/x.json")
    monkeypatch.setattr(E, "fetch_ended", boom)
    d = E.build(today=today)
    assert d["meta"]["n_ended"] == 3 and d["meta"]["errors"] == ["청약종료 202610: OSError: 연결 끊김 x.json"]
    monkeypatch.setattr(E, "CACHE", E.CACHE.parent / "empty")
    monkeypatch.setattr(E, "fetch_subscribing", lambda: [])
    with pytest.raises(RuntimeError):
        E.build(today=today)


def test_run_skips_when_fresh(fake):
    E.OUT.write_text(json.dumps({"generated_at": dt.datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    assert E.run().startswith("건너뜀") and fake == []


def test_api_els_default_saved_and_lan(fake, monkeypatch):
    c = web.app.test_client()
    assert c.get("/api/els", **LOCAL).get_json() == {"generated_at": None, "meta": {}, "method": None, "subscribing": [], "ended": []}
    doc = {"generated_at": "2000-01-01T00:00:00", "meta": {"errors": ["청약종료 202610: OSError: No such file: '/home/me/p/data/x.json'"]},
           "method": "m", "subscribing": [{"code": "X"}], "ended": []}
    E.OUT.write_text(json.dumps(doc), encoding="utf-8")
    assert c.get("/api/els", **LOCAL).get_json() == doc
    assert web._lan_get_ok("/api/els") and not any("/api/els".startswith(h) for h in config.LAN_HIDE)
    monkeypatch.setattr(config, "LAN_SHARE", True)
    monkeypatch.setattr(config, "LAN_ALLOW", ["10.0.0.0/24"])
    monkeypatch.setitem(web.LAN, "key", KEY)
    monkeypatch.setattr(web, "ALLOWED_HOSTS", web.ALLOWED_HOSTS | {"10.0.0.5:5101"})
    c = web.app.test_client()
    c.set_cookie("rc_key", KEY, domain="10.0.0.5")
    r = c.get("/api/els", environ_base={"REMOTE_ADDR": "10.0.0.9"}, headers={"Host": "10.0.0.5:5101"})
    assert r.status_code == 200 and r.get_json()["meta"]["errors"] == ["청약종료 202610: OSError: No such file: 'x.json'"]


def test_share_snapshot_and_refresh_step(monkeypatch, tmp_path):
    import share_site as S

    def fake_get(path):
        if path == "/api/backtest/catalog":
            return {"presets": [{"value": k, "start": None, "end": None} for k in S.PRESETS], "strategies": []}
        return {}
    monkeypatch.setattr(S, "get", fake_get)
    monkeypatch.setattr(S, "post", lambda path, body: {})
    monkeypatch.setattr(S, "ROOT", tmp_path)
    snap, _ = S.collect()
    assert "/api/els" in snap["get"]
    full = [n for n, _ in refresh_all.steps(False)]
    assert "공모 ELS(금투협)" in full and "공모 ELS(금투협)" not in [n for n, _ in refresh_all.steps(True)]
