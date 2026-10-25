# -*- coding: utf-8 -*-
"""한 파일 스냅샷(share_site --file) 조립 회귀 테스트 — 서버·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_site as S  # noqa: E402

SNAP = {"at": "2026-10-25 09:00", "get": {"/api/report/x": {"content": "a</script><script>alert(1)</script><!-- b"}}, "monthly": {}}
BT = {"run": {"k|1y": {"note": "</SCRIPT>"}}, "all": {}}


def _blocks(html):
    return dict(re.findall(r'<script type="application/json" id="([^"]+)">(.*?)</script>', html, re.S))


def test_json_block_escapes_script_end_and_roundtrips():
    b = S.json_block("snap.json", SNAP)
    body = b[len('<script type="application/json" id="snap.json">'):-len("</script>")]
    assert "<" not in body                       # </script>·<!-- 가 블록을 끊지 못한다
    assert json.loads(body) == SNAP              # 값은 그대로


def test_file_html_is_self_contained():
    html = S.file_html(SNAP, BT)
    bl = _blocks(html)
    assert json.loads(bl["snap.json"]) == SNAP and json.loads(bl["bt.json"]) == BT
    for bad in ("127.0.0.1", 'src="static/', "static/avatars/", "static/vendor/", "realFetch(f"):
        assert bad not in html, bad
    assert html.count("data:image/svg+xml;base64,") >= 3
    assert "보기 전용 스냅샷" in html and "2026-10-25 09:00" in html and "라이선스" in html


def test_file_html_org_flag():
    assert '<span class="org">한화생명</span>' in S.file_html(SNAP, BT)
    assert '<span class="org">한화생명</span>' not in S.file_html(SNAP, BT, keep_org=False)


def test_scrub_keeps_only_file_names_of_local_paths():
    d = {"wb": r"\\wsl.localhost\Ubuntu\home\me\Stock_Dashboard\data\b.xlsx", "t": ["저장 /home/me/a/c.md 끝", r"C:\Users\me\x.txt"], "n": "3/4 정상"}
    assert S.scrub(d) == {"wb": "b.xlsx", "t": ["저장 c.md 끝", "x.txt"], "n": "3/4 정상"}


def test_scrub_paths_with_spaces_and_other_drives_but_not_urls():
    """공백 있는 폴더·C:\\Users 밖 드라이브·/mnt·네트워크 경로도 파일 이름만 — https:// 주소와 '3/4' 같은 글은 그대로."""
    d = [r"D:\work\my proj\data\positions.json", "열기 실패: " + repr(r"C:\Program Files\x\a b.xlsx"),
         "/mnt/c/Users/me/My Documents/r.md 저장", r"\\fileserver\share\팀 폴더\a.xlsx",
         "https://example.com/home/x · http://h/mnt/y", "3/4 정상"]
    assert S.scrub(d) == ["positions.json", "열기 실패: 'a b.xlsx'", "r.md 저장", "a.xlsx",
                          "https://example.com/home/x · http://h/mnt/y", "3/4 정상"]


def test_build_docs_scrubs_local_paths(monkeypatch, tmp_path):
    """공개 페이지(docs/)에도 이 PC 폴더 경로가 남지 않는다 — 한 파일본(build_file)과 같게."""
    snap = {"at": "2026-10-25 09:00", "get": {"/api/book": {"positions_path": r"C:\Users\me\proj\data\positions.json"}}, "monthly": {}}
    monkeypatch.setattr(S, "collect", lambda: (snap, {"run": {"k|1y": {"note": "/home/me/proj/reports/a.md"}}, "all": {}}))
    monkeypatch.setattr(S, "OUT", tmp_path)
    monkeypatch.setattr(S, "write_html", lambda at: None)
    monkeypatch.setattr(S.shutil, "copytree", lambda *a, **k: None)
    S.build()
    out = (tmp_path / "snap.json").read_text(encoding="utf-8") + (tmp_path / "bt.json").read_text(encoding="utf-8")
    assert "Users" not in out and "/home/" not in out
    assert "positions.json" in out and "a.md" in out


def test_collect_skips_lan_hidden_paths(monkeypatch, tmp_path):
    """사내 공유 보기에서 숨기는 화면(config.LAN_HIDE)은 공유본에도 담지 않는다(서버에 묻지도 않는다)."""
    calls = []

    def fake_get(path):
        calls.append(path)
        if path == "/api/backtest/catalog":
            return {"presets": [{"value": k, "start": None, "end": None} for k in S.PRESETS], "strategies": []}
        return {}

    monkeypatch.setattr(S.config, "LAN_HIDE", ["/api/universe/spark"])
    monkeypatch.setattr(S, "ROOT", tmp_path)   # /api/monthly 보고서 되돌리기가 저장소 파일을 건드리지 않게
    monkeypatch.setattr(S, "get", fake_get)
    monkeypatch.setattr(S, "post", lambda path, body: {})
    snap, _ = S.collect()
    assert "/api/universe/spark" not in calls and "/api/universe/spark" not in snap["get"]
    assert "/api/overview" in snap["get"]


def test_gitignore_keeps_reports_mp_venv_local():
    lines = {x.strip() for x in (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert {"reports/", "data/mp/", ".venv/"} <= lines
