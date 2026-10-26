# -*- coding: utf-8 -*-
"""증시자금(kofia_free — 금투협 FreeSIS) 회귀 테스트 — 합성 데이터만, 네트워크 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kofia_free as K  # noqa: E402

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
