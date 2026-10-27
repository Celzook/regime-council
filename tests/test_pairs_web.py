# -*- coding: utf-8 -*-
"""페어트레이딩 화면 연결 회귀 테스트 — 조회 API·사내 공유 허용·자동 갱신 단계·공유본 수집. 서버·네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import pairs  # noqa: E402
import refresh_all  # noqa: E402
import web  # noqa: E402

LOCAL = {"environ_base": {"REMOTE_ADDR": "127.0.0.1"}, "headers": {"Host": "127.0.0.1:5101"}}
PID = "005935.KS~005930.KS"
DOC = {"generated_at": "2000-01-01T00:00:00", "meta": {"errors": [], "n_adopted_last": 1, "n_candidates": {"보통·우선": 1}},
       "pairs": [{"id": PID, "state": "대기", "z": 0.4}], "backtest": {"overall": {"n": 0}}, "missing": []}
CHART = {PID: {"dates": ["2026-10-26"], "z": [0.4], "s": [0.01], "beta": [], "trades": []}}


@pytest.fixture()
def files(monkeypatch, tmp_path):
    monkeypatch.setattr(pairs, "OUT", tmp_path / "pairs.json")
    monkeypatch.setattr(pairs, "CHARTS", tmp_path / "pairs_charts.json")
    return tmp_path


def _write(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_pairs_endpoint_and_chart(files):
    _write(pairs.OUT, DOC)
    _write(pairs.CHARTS, CHART)
    c = web.app.test_client()
    assert c.get("/api/timing/pairs", **LOCAL).get_json() == DOC
    r = c.get("/api/timing/pairs/" + PID.replace("~", "%7E"), **LOCAL)
    assert r.status_code == 200 and r.get_json() == CHART[PID]
    assert c.get("/api/timing/pairs/000000.KS~000001.KS", **LOCAL).status_code == 404


@pytest.mark.parametrize("broken", [False, True])
def test_pairs_default_shape_when_missing_or_broken(files, broken):
    if broken:
        pairs.OUT.write_text("{", encoding="utf-8")
        pairs.CHARTS.write_text("{", encoding="utf-8")
    c = web.app.test_client()
    assert c.get("/api/timing/pairs", **LOCAL).get_json() == {"generated_at": None, "meta": {}, "pairs": [], "backtest": {}, "missing": []}
    assert c.get("/api/timing/pairs/" + PID, **LOCAL).status_code == 404


@pytest.fixture()
def lan(monkeypatch):
    """사내 공유 보기(동료 PC) — test_timing_web.lan 과 같은 설정."""
    key = "k" * 20
    monkeypatch.setattr(config, "LAN_SHARE", True)
    monkeypatch.setattr(config, "LAN_ALLOW", ["10.0.0.0/24"])
    monkeypatch.setitem(web.LAN, "key", key)
    monkeypatch.setattr(web, "ALLOWED_HOSTS", web.ALLOWED_HOSTS | {"10.0.0.5:5101"})
    c = web.app.test_client()
    c.set_cookie("rc_key", key, domain="10.0.0.5")
    return c


def test_pairs_lan_allowed_and_paths_scrubbed(lan, files):
    for p in ("/api/timing/pairs", "/api/timing/pairs/" + PID):
        assert web._lan_get_ok(p) and not any(p.startswith(h) for h in config.LAN_HIDE)
    doc = json.loads(json.dumps(DOC))
    doc["meta"]["errors"] = ["가격: [Errno 2] No such file: '/home/me/proj/data/history/pairs_ret_daily.csv'"]
    _write(pairs.OUT, doc)
    _write(pairs.CHARTS, CHART)
    env = {"environ_base": {"REMOTE_ADDR": "10.0.0.9"}, "headers": {"Host": "10.0.0.5:5101"}}
    r = lan.get("/api/timing/pairs", **env)
    assert r.status_code == 200
    assert r.get_json()["meta"]["errors"] == ["가격: [Errno 2] No such file: 'pairs_ret_daily.csv'"]
    assert lan.get("/api/timing/pairs/" + PID, **env).status_code == 200
    local = web.app.test_client().get("/api/timing/pairs", **LOCAL).get_json()
    assert "/home/me/proj" in local["meta"]["errors"][0]   # 이 PC 화면은 그대로


def test_refresh_step_order_and_skip(monkeypatch):
    names = [n for n, _ in refresh_all.steps(False)]
    assert names.index("과거 시계열") < names.index("페어트레이딩") < names.index("데이터 점검")
    assert "페어트레이딩" not in [n for n, _ in refresh_all.steps(True)]
    calls = []
    monkeypatch.setattr(pairs, "build", lambda *a, **k: calls.append(1) or {"meta": {"n_adopted_last": 3, "n_candidates": {"a": 2, "b": 5}}})
    monkeypatch.setattr(pairs, "age_hours", lambda: 1.0)
    assert refresh_all._pairs_if_stale().startswith("건너뜀") and not calls
    monkeypatch.setattr(pairs, "age_hours", lambda: None)
    assert refresh_all._pairs_if_stale() == "채택 3쌍 · 후보 7쌍" and calls == [1]


def test_pairs_outputs_are_gitignored():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for f in ("data/pairs.json", "data/pairs_charts.json", "data/pairs_master.json", "data/.api_keys.json"):
        assert f in gi
