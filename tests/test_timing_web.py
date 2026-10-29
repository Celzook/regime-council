# -*- coding: utf-8 -*-
"""마켓타이밍 화면 연결(W4) 회귀 테스트 — 조회 API·사내 공유 허용·자동 갱신 단계·공유본 수집. 서버·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import refresh_all  # noqa: E402
import spikes  # noqa: E402
import timing  # noqa: E402
import web  # noqa: E402

KEY = "k" * 20
LOCAL = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}, "headers": {"Host": "127.0.0.1:5101"}}
TIMING_DOC = {"generated_at": "2000-01-01T00:00:00", "meta": {"errors": [], "n_ok": 1}, "method": "m",
              "composite": {"score": 0.11, "label": "중립"}, "signals": [{"key": "vix", "name": "VIX", "ok": True}],
              "phase2": [], "notes": ["n"]}
SPIKES_DOC = {"generated_at": "2000-01-01T00:00:01", "meta": {"notes": ["매매 지시 아님"]}, "triggers": {"vix30": {"name": "VIX ≥ 30"}},
              "studies": [{"id": "vix30_QQQ", "verdict": "v", "is_signal": True}], "missing": []}


@pytest.fixture()
def files(monkeypatch, tmp_path):
    """두 모듈의 출력 파일을 임시 폴더로 — 실제 data/ 는 읽지도 쓰지도 않는다."""
    monkeypatch.setattr(timing, "OUT", tmp_path / "timing.json")
    monkeypatch.setattr(spikes, "OUT", tmp_path / "spikes.json")
    return tmp_path


def _write(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_endpoints_return_saved_files(files):
    _write(timing.OUT, TIMING_DOC)
    _write(spikes.OUT, SPIKES_DOC)
    c = web.app.test_client()
    a, b = c.get("/api/timing", **LOCAL), c.get("/api/timing/spikes", **LOCAL)
    assert a.status_code == 200 and a.get_json() == TIMING_DOC
    assert b.status_code == 200 and b.get_json() == SPIKES_DOC


@pytest.mark.parametrize("broken", [False, True])
def test_endpoints_default_shape_when_missing_or_broken(files, broken):
    """파일이 없거나 깨졌으면 화면이 읽는 키를 모두 가진 빈 문서 — 화면은 '아직 계산 결과 없음'을 띄운다."""
    if broken:
        timing.OUT.write_text("{", encoding="utf-8")
        spikes.OUT.write_text("{", encoding="utf-8")
    c = web.app.test_client()
    a, b = c.get("/api/timing", **LOCAL).get_json(), c.get("/api/timing/spikes", **LOCAL).get_json()
    assert a == {"generated_at": None, "meta": {}, "method": None, "composite": {}, "signals": [], "phase2": [], "notes": []}
    assert b == {"generated_at": None, "meta": {}, "triggers": {}, "studies": [], "missing": []}


def test_endpoints_are_lan_view_allowed_and_not_hidden():
    for p in ("/api/timing", "/api/timing/spikes"):
        assert p in config.LAN_GET_ALLOW and web._lan_get_ok(p)
        assert not any(p.startswith(h) for h in config.LAN_HIDE)


@pytest.fixture()
def lan(monkeypatch):
    monkeypatch.setattr(config, "LAN_SHARE", True)
    monkeypatch.setattr(config, "LAN_ALLOW", ["10.0.0.0/24"])
    monkeypatch.setitem(web.LAN, "key", KEY)
    monkeypatch.setattr(web, "ALLOWED_HOSTS", web.ALLOWED_HOSTS | {"10.0.0.5:5101"})
    c = web.app.test_client()
    c.set_cookie("rc_key", KEY, domain="10.0.0.5")
    return c


def _get(c, path):
    return c.get(path, environ_base={"REMOTE_ADDR": "10.0.0.9"}, headers={"Host": "10.0.0.5:5101"})


def test_lan_view_reads_both_and_hides_local_paths(lan, files):
    """동료 화면(사내 공유 보기)도 두 화면을 본다 — 입력 오류 문장 속 이 PC 폴더 경로는 파일 이름만."""
    doc = json.loads(json.dumps(TIMING_DOC))
    doc["meta"]["errors"] = ["equity_daily: [Errno 13] Permission denied: '/home/me/proj/data/history/equity_daily.csv'"]
    _write(timing.OUT, doc)
    _write(spikes.OUT, SPIKES_DOC)
    a = _get(lan, "/api/timing")
    assert a.status_code == 200
    assert a.get_json()["meta"]["errors"] == ["equity_daily: [Errno 13] Permission denied: 'equity_daily.csv'"]
    assert _get(lan, "/api/timing/spikes").get_json() == SPIKES_DOC
    local = web.app.test_client().get("/api/timing", **LOCAL).get_json()
    assert "/home/me/proj" in local["meta"]["errors"][0]   # 이 PC 화면은 그대로


def test_lan_view_still_blocks_post_on_timing(lan):
    r = lan.post("/api/timing", environ_base={"REMOTE_ADDR": "10.0.0.9"}, headers={"Host": "10.0.0.5:5101"})
    assert r.status_code == 403


def test_screen_share_of_timing_tab_follows_refinitiv_flag(monkeypatch):
    """두 화면은 Refinitiv 파생 — 대화 '화면 공유'는 SHARE_REFINITIV_DATA 가 False 면 본문을 보내지 않는다(인포맥스 탭 아님)."""
    import voice
    scr = {"tab": "timing", "label": "마켓타이밍", "text": "VKOSPI 11.11"}
    assert "timing" not in config.VENDOR_SCREEN_TABS
    monkeypatch.setattr(config, "SHARE_REFINITIV_DATA", False)
    assert "VKOSPI 11.11" not in voice.screen_block(scr) and "외부 전송 제한" in voice.screen_block(scr)
    monkeypatch.setattr(config, "SHARE_REFINITIV_DATA", True)
    assert "VKOSPI 11.11" in voice.screen_block(scr)


def test_agent_contexts_do_not_read_timing_files():
    """에이전트 컨텍스트(agent·voice·daily·brief)는 두 파일을 읽지 않는다 — 화면 전용."""
    for f in ("agent.py", "voice.py", "daily.py", "brief.py", "council.py", "qa.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert "import timing" not in src and "import spikes" not in src and "spikes.json" not in src and "timing.json" not in src, f


def test_refresh_steps_order():
    names = [n for n, _ in refresh_all.steps(False)]
    i = names.index
    assert i("밸류에이션 이력") < i("변동성 급등") < i("마켓타이밍 시그널")
    assert i("시장 스냅샷") < i("마켓타이밍 시그널") and i("주식 대시보드") < i("마켓타이밍 시그널")
    assert i("변동성 급등") < i("옵션 풋/콜·미결제약정") < i("마켓타이밍 시그널") and i("증시자금(금투협)") < i("마켓타이밍 시그널")
    assert i("과거 시계열") < i("위탁운용사 펀드") and i("주식 대시보드") < i("위탁운용사 펀드")   # BM 이력(kofia·equity_daily) 갱신 뒤
    assert i("인포맥스·매크로") < i("외국인 선물(인포맥스)") < i("마켓타이밍 시그널")   # 숨김 엑셀은 한 번에 하나(순서대로)
    quick = [n for n, _ in refresh_all.steps(True)]
    assert not {"변동성 급등", "마켓타이밍 시그널", "위탁운용사 펀드", "옵션 풋/콜·미결제약정", "증시자금(금투협)",
                "외국인 선물(인포맥스)"} & set(quick)


def test_spikes_step_skips_when_fresh(monkeypatch):
    calls = []
    monkeypatch.setattr(spikes, "build", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(spikes, "age_hours", lambda: 3.0)
    assert refresh_all._spikes_if_stale().startswith("건너뜀") and not calls
    for age in (None, spikes.STALE_HOURS + 1):
        monkeypatch.setattr(spikes, "age_hours", lambda a=age: a)
        assert refresh_all._spikes_if_stale() == "갱신"
    assert len(calls) == 2


def test_timing_step_reports_composite(monkeypatch):
    monkeypatch.setattr(timing, "build", lambda *a, **k: {"composite": {"label": "중립"}})
    assert refresh_all._timing() == "합성 중립"


def test_refresh_failure_does_not_stop_later_steps(monkeypatch, tmp_path):
    """변동성 급등 단계가 실패해도 시그널 단계는 돈다(기존 동작)."""
    import refinitiv_src
    ran = []

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(refresh_all, "steps", lambda quick: [("변동성 급등", boom), ("마켓타이밍 시그널", lambda: ran.append(1) or "합성 중립")])
    monkeypatch.setattr(refresh_all, "LOG_DIR", tmp_path)
    monkeypatch.setattr(refresh_all, "STATUS", tmp_path / "refresh_status.json")
    monkeypatch.setattr(refresh_all, "notify", lambda *a: None)
    monkeypatch.setattr(refinitiv_src, "available", lambda force=False: True)
    monkeypatch.setattr(sys, "argv", ["refresh_all.py"])
    assert refresh_all.main() == 1 and ran == [1]
    st = json.loads((tmp_path / "refresh_status.json").read_text(encoding="utf-8"))
    assert st["failed"] == ["변동성 급등"] and st["steps"][1]["ok"]


def test_share_snapshot_collects_timing_screens(monkeypatch, tmp_path):
    """공유본(share_site)은 GET 주소를 목록으로 모은다 — 두 화면이 들어가는지(LAN_HIDE 가 아니므로)."""
    import share_site as S
    got = []

    def fake_get(path):
        got.append(path)
        if path == "/api/backtest/catalog":
            return {"presets": [{"value": k, "start": None, "end": None} for k in S.PRESETS], "strategies": []}
        return {}
    monkeypatch.setattr(S, "get", fake_get)
    monkeypatch.setattr(S, "post", lambda path, body: {})
    monkeypatch.setattr(S, "ROOT", tmp_path)   # 월간 보고 파일(reports/)은 임시 폴더에서만
    snap, _ = S.collect()
    assert "/api/timing" in got and "/api/timing/spikes" in got
    assert "/api/timing" in snap["get"] and "/api/timing/spikes" in snap["get"]
