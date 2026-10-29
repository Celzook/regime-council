# -*- coding: utf-8 -*-
"""refinitiv_src 수집 경로 — 네트워크 없이 합성 데이터로."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import refinitiv_src as R  # noqa: E402


def test_refresh_frame_offline_still_updates_yf_columns(tmp_path, monkeypatch):
    """Refinitiv 가 끊겨도 yfinance 열(^VIX 등)은 갱신하고, Refinitiv 열은 손대지 않는다."""
    idx = pd.bdate_range("2026-01-01", periods=5)
    path = tmp_path / "t.csv"
    pd.DataFrame({"^VIX": [20.0] * 5, "SPY": [500.0] * 5}, index=idx).rename_axis("date").to_csv(path)
    new = pd.Series([21.0, 22.0], index=pd.bdate_range(idx[-1] + pd.Timedelta(days=1), periods=2), name="^VIX")

    monkeypatch.setattr(R, "available", lambda force=False: False)
    monkeypatch.setattr(R, "_yf_history", lambda t, since: new)
    monkeypatch.setattr(R, "history", lambda *a, **k: pytest.fail("오프라인에 Refinitiv 호출"))
    df = R.refresh_frame(path, ["^VIX", "SPY"], "2026-01-01")
    assert df["^VIX"].dropna().iloc[-1] == 22.0
    assert df["SPY"].dropna().iloc[-1] == 500.0 and df["SPY"].last_valid_index() == idx[-1]


def test_refresh_frame_offline_without_yf_columns_returns_old(tmp_path, monkeypatch):
    path = tmp_path / "t.csv"
    old = pd.DataFrame({"SPY": [1.0, 2.0]}, index=pd.bdate_range("2026-01-01", periods=2)).rename_axis("date")
    old.to_csv(path)
    before = path.read_bytes()
    monkeypatch.setattr(R, "available", lambda force=False: False)
    monkeypatch.setattr(R, "history", lambda *a, **k: pytest.fail("오프라인에 Refinitiv 호출"))
    df = R.refresh_frame(path, ["SPY"], "2026-01-01")
    assert list(df["SPY"]) == [1.0, 2.0] and path.read_bytes() == before


class _Sock:
    """connect_ex 결과만 흉내 — OPEN 에 있는 포트만 열림."""
    OPEN: set = set()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def connect_ex(self, addr):
        return 0 if addr[1] in self.OPEN else 111


def _fake_urlopen(bodies):
    import io
    import urllib.error

    def urlopen(url, timeout=None):
        port = int(url.split(":")[2].split("/")[0])
        b = bodies[port]
        if isinstance(b, int):
            raise urllib.error.HTTPError(url, b, "x", None, None)
        return io.BytesIO(b)
    return urlopen


def test_proxy_port_skips_open_port_that_is_not_workspace(monkeypatch):
    """재로그인 뒤 프록시가 9002 로 옮겨 가고 9001 은 Windows 시스템이 404 로 열어 둔 상태(2026-10-29 실측)."""
    _Sock.OPEN = {9001, 9002}
    monkeypatch.setattr(R.socket, "socket", _Sock)
    monkeypatch.setattr(R.urllib.request, "urlopen",
                        _fake_urlopen({9001: 404, 9002: b'{"statusCode":"ST_PROXY_READY","version":"3.12.0"}'}))
    assert R._proxy_port() == 9002


def test_proxy_port_none_when_nothing_answers(monkeypatch):
    _Sock.OPEN = {9001}
    monkeypatch.setattr(R.socket, "socket", _Sock)
    monkeypatch.setattr(R.urllib.request, "urlopen", _fake_urlopen({9001: 404}))
    assert R._proxy_port() is None
    monkeypatch.setattr(R, "_STATE", {"ok": None, "checked": None, "error": ""})
    assert R.available(force=True) is False and "응답 없음" in R.status()["error"]
