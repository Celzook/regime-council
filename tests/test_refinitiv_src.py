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
