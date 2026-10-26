# -*- coding: utf-8 -*-
"""옵션 풋/콜·선물 미결제약정 수집(options_pc) 회귀 테스트 — 합성 체인만, Refinitiv·네트워크·data/history 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import datetime
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import options_pc as O  # noqa: E402

AFTER = datetime.datetime(2026, 10, 26, 16, 30)


def _chain(td="2026-10-26") -> pd.DataFrame:
    """기초지수 줄 + 근월(11-12)·원월(12-10) 콜·풋. 오래 거래 없던 행사가 하나(TRADE_DATE 10-20)는 거래량을 세지 않는다."""
    rows = [("/.KS200", None, None, 999, None, None, td),
            ("/KS200700K6.KS", "CALL", "2026-11-12", 30, 100, td, td),
            ("/KS200700W6.KS", "PUT ", "2026-11-12", 10, 300, td, td),
            ("/KS200710K6.KS", "CALL", "2026-11-12", 77, 50, td, "2026-10-20"),   # 옛 거래량 — 빼야 함
            ("/KS200700L6.KS", "CALL", "2026-12-10", 5, 40, td, td),
            ("/KS200700X6.KS", "PUT ", "2026-12-10", 15, 60, td, td)]
    return pd.DataFrame(rows, columns=["Instrument", "PUTCALLIND", "EXPIR_DATE", "ACVOL_1", "OPINT_1", "OPINT_DATE",
                                       "TRADE_DATE"])


def test_summarize_sums_by_side_and_skips_underlying_and_stale_volume():
    td, row = O.summarize(_chain(), AFTER)
    assert td == pd.Timestamp("2026-10-26")
    assert row["call_vol"] == 35 and row["put_vol"] == 25            # 기초지수 999·옛 거래량 77 제외
    assert row["call_oi"] == 190 and row["put_oi"] == 360            # 미결제약정은 그날 기준이면 모두
    assert row["front_expiry"] == "2026-11-12"
    assert (row["front_call_vol"], row["front_put_vol"], row["front_call_oi"], row["front_put_oi"]) == (30, 10, 150, 300)
    assert row["n_series"] == 5


def test_summarize_intraday_returns_none_and_morning_keeps_previous_day():
    assert O.summarize(_chain(), datetime.datetime(2026, 10, 26, 14, 0)) is None     # 장중 누적 — 쓰지 않음
    td, _ = O.summarize(_chain("2026-10-23"), datetime.datetime(2026, 10, 26, 8, 0))  # 개장 전 = 직전 거래일 확정치
    assert td == pd.Timestamp("2026-10-23")


def test_summarize_oi_left_empty_when_oi_date_differs():
    df = _chain()
    df["OPINT_DATE"] = "2026-10-23"
    _, row = O.summarize(df, AFTER)
    assert row["put_oi"] is None and row["call_oi"] is None and row["call_vol"] == 35


def test_summarize_rejects_empty_volume():
    df = _chain()
    df["ACVOL_1"] = 0
    with pytest.raises(ValueError):
        O.summarize(df, AFTER)


def test_upsert_one_row_per_date(tmp_path):
    p = tmp_path / "putcall_daily.csv"
    td, row = O.summarize(_chain(), AFTER)
    O.upsert(p, td, row)
    O.upsert(p, td, {**row, "put_vol": 99})                           # 같은 날 다시 → 덮어씀
    O.upsert(p, pd.Timestamp("2026-10-23"), row)
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-10-23", "2026-10-26"]
    assert df.loc["2026-10-26", "put_vol"] == 99 and list(df.columns) == O.PC_COLS


def test_oi_frame_drops_expiry_day_and_sums():
    p = pd.DataFrame({"KSc1": [100.0, 0.0, 120.0, 130.0], "KSc2": [5.0, 90.0, 6.0, None]},
                     index=pd.bdate_range("2026-09-09", periods=4))
    f = O.oi_frame(p)
    assert list(f.index.strftime("%m-%d")) == ["09-09", "09-11"] and f["total"].tolist() == [105.0, 126.0]


def test_run_keeps_one_side_when_other_fails(monkeypatch):
    monkeypatch.setattr(O.R, "available", lambda force=False: True)
    monkeypatch.setattr(O, "collect_putcall", lambda: "풋콜 OK")

    def boom(full=False):
        raise RuntimeError("값 없음")
    monkeypatch.setattr(O, "refresh_oi", boom)
    assert O.run().startswith("풋콜 OK") and "미결제약정 실패" in O.run()
    monkeypatch.setattr(O, "collect_putcall", lambda: (_ for _ in ()).throw(RuntimeError("체인")))
    with pytest.raises(RuntimeError):
        O.run()
