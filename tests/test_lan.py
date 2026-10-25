# -*- coding: utf-8 -*-
"""사내 공유 보기(REGIME_LAN) 조회 허용 목록 회귀 테스트 — 서버·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import web  # noqa: E402

KEY = "k" * 20


def _spa_get_paths() -> set[str]:
    """static/index.html 이 api('/api/…') 로 부르는 GET 주소 — 뒤에 + 로 붙는 부분(아이디)은 'x' 로, 쿼리는 뺀다."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    calls = list(re.finditer(r"\bapi\('(/api/[^']*)'", html))
    # 화면이 api() 말고 다른 길(fetch·다른 따옴표)로 /api/ 를 부르면 이 검사가 놓치므로 여기서 멈춘다
    assert html.count("'/api/") == len(calls) and '"/api/' not in html and "`/api/" not in html
    out = set()
    for m in calls:
        i, depth = html.index("(", m.start()) + 1, 1   # api( 의 닫는 괄호까지 = 이 호출의 인자
        while depth:
            depth += {"(": 1, ")": -1}.get(html[i], 0)
            i += 1
        args = html[m.start():i]
        if re.search(r"method\s*:\s*'(?!GET')", args):   # POST 는 LAN_POST_ALLOW 가 따로 막는다
            continue
        path = m.group(1)
        if "?" in path:
            out.add(path.split("?")[0])
        else:
            out.add(path + "x" if html[m.end():].lstrip().startswith("+") else path)
    return out


def test_every_spa_get_path_is_allowed_or_hidden():
    """화면 탭이 부르는 조회 주소는 모두 허용 목록에 있거나 일부러 숨긴(LAN_HIDE) 것 — 빠뜨리면 동료 화면이 조용히 깨진다."""
    paths = _spa_get_paths()
    assert {"/api/overview", "/api/checks/x", "/api/jobs/x", "/api/universe/spark"} <= paths   # 파서가 비어 돌지 않게
    for p in sorted(paths):
        assert web._lan_get_ok(p) or any(p.startswith(h) for h in config.LAN_HIDE), p


def test_allow_list_entries_are_real_get_routes():
    adapter = web.app.url_map.bind("127.0.0.1:5101")
    for x in config.LAN_GET_ALLOW:
        adapter.match(x[:-1] + "x" if x.endswith("*") else x, method="GET")   # 오타면 NotFound


@pytest.fixture()
def lan(monkeypatch):
    monkeypatch.setattr(config, "LAN_SHARE", True)
    monkeypatch.setattr(config, "LAN_ALLOW", ["10.0.0.0/24"])
    monkeypatch.setitem(web.LAN, "key", KEY)
    monkeypatch.setattr(web, "ALLOWED_HOSTS", web.ALLOWED_HOSTS | {"10.0.0.5:5101"})
    c = web.app.test_client()
    c.set_cookie("rc_key", KEY, domain="10.0.0.5")   # 키를 쿠키로 옮긴 뒤의 동료 브라우저
    return c


def _get(c, path):
    return c.get(path, environ_base={"REMOTE_ADDR": "10.0.0.9"}, headers={"Host": "10.0.0.5:5101"})


@pytest.mark.parametrize("path", ["/api/positions", "/api/jobs", "/api/jobs/x", "/api/positions?key=" + KEY])
def test_lan_blocks_paths_outside_allow_list(lan, path):
    r = _get(lan, path)
    assert r.status_code == 403 and "볼 수 없는 화면" in r.get_json()["error"]


@pytest.fixture()
def macro(monkeypatch, tmp_path):
    import infomax_bridge
    monkeypatch.setattr(infomax_bridge, "BRIDGE", tmp_path / "인포맥스_브리지.xlsx")   # 없음 → 숨김 엑셀 갱신을 띄우지 않는다
    wb = r"C:\Users\me\proj\data\인포맥스_브리지.xlsx"
    monkeypatch.setattr(infomax_bridge, "load", lambda: {"meta": {"mode": "저장값(파일)", "workbook": wb}, "series": [], "calendar": []})
    return wb


def test_lan_allowed_tab_api_hides_local_paths(lan, macro):
    r = _get(lan, "/api/macro")
    assert r.status_code == 200
    d = r.get_json()
    assert d["meta"]["workbook"] == "인포맥스_브리지.xlsx" and d["bridge_path"] == ""
    assert _get(lan, "/").status_code == 200


def test_local_macro_keeps_full_workbook_path(macro):
    r = web.app.test_client().get("/api/macro", environ_base={"REMOTE_ADDR": "127.0.0.1"}, headers={"Host": "127.0.0.1:5101"})
    assert r.status_code == 200 and r.get_json()["meta"]["workbook"] == macro


@pytest.mark.parametrize("ev_fails", [True, False])
def test_lan_overview_hides_local_paths(lan, monkeypatch, ev_fails):
    """홈 화면(/api/overview) — 일일 점검 기록의 보유·한도 파일 경로와 OSError 경고 속 경로는 원격에 파일 이름만(두 응답 분기 모두)."""
    import agent
    import limits
    import store
    pos, lim = r"C:\Users\me\my proj\data\positions.json", "/home/me/proj/data/limits.json"
    monkeypatch.setattr(store, "latest_daily", lambda: {"day": "2026-10-26", "book": {"positions_path": pos, "limits_path": lim, "positions": 3}})
    monkeypatch.setitem(agent.LOAD_ERRORS, "x.json", "PermissionError: [Errno 13] Permission denied: " + repr(r"D:\work\a b\data\x.json"))
    monkeypatch.setattr(limits, "load_limits", lambda: {})
    monkeypatch.setattr(store, "load_mp", lambda *a, **k: None)
    monkeypatch.setattr(web, "_data_feed", lambda: {})   # Refinitiv 연결 확인을 하지 않게
    if ev_fails:
        def boom(**k):
            raise FileNotFoundError(2, "No such file or directory", pos)
        monkeypatch.setattr(limits, "evaluate", boom)
    else:
        monkeypatch.setattr(limits, "evaluate", lambda **k: {"positions": []})
    r = _get(lan, "/api/overview")
    assert r.status_code == 200
    txt, d = r.get_data(as_text=True), r.get_json()
    assert "Users" not in txt and "/home/" not in txt and ":\\\\" not in txt and "a b" not in txt
    assert d["latest_daily"]["book"] == {"positions_path": "positions.json", "limits_path": "limits.json", "positions": 3}
    assert any("'x.json'" in w for w in d["data_warnings"])
    local = web.app.test_client().get("/api/overview", environ_base={"REMOTE_ADDR": "127.0.0.1"}, headers={"Host": "127.0.0.1:5101"})
    assert local.get_json()["latest_daily"]["book"]["positions_path"] == pos   # 이 PC 화면은 그대로


def test_book_mark_skipped_ends_as_error(monkeypatch, tmp_path):
    """'시가 평가' — book.sync 가 skipped(미연결·가격 0건)를 돌려주면 작업은 실패로 끝나 화면이 '완료'로 띄우지 않는다."""
    import time
    import book
    monkeypatch.setattr(book, "DEFAULT_BOOK", tmp_path / "시험포트.xlsx")
    book.DEFAULT_BOOK.write_bytes(b"")
    monkeypatch.setattr(book, "sync", lambda **k: {"book": "시험포트.xlsx", "skipped": True, "error": "Refinitiv 미연결 — 기존 positions.json 유지"})
    c = web.app.test_client()
    r = c.post("/api/book/mark", environ_base={"REMOTE_ADDR": "127.0.0.1"}, headers={"Host": "127.0.0.1:5101"})
    assert r.status_code == 200
    jid = r.get_json()["job_id"]
    for _ in range(100):
        if web.JOBS[jid]["status"] != "running":
            break
        time.sleep(0.05)
    assert web.JOBS[jid]["status"] == "error" and "Refinitiv 미연결" in web.JOBS[jid]["error"]
