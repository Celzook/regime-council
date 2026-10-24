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
