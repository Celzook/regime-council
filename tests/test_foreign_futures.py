# -*- coding: utf-8 -*-
"""외국인 KOSPI200 선물 순매수 수집(foreign_futures) 회귀 테스트 — 합성 시트 값만, 인포맥스·엑셀 없이 돈다.
실행: python -m pytest tests   (Windows 파이썬)
"""
import datetime
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import foreign_futures as F  # noqa: E402

# 숨김 엑셀이 돌려준 '이력' 시트 모양(2026-10-29 실측): 수식 칸 '조회[코드]', 빈 줄, 그 아래 엑셀 일련번호·수량·금액
ROWS = [["조회[A016C000]", None, None], [None, None, None],
        [45294, -16516, -1473856650000], [45295, -3347, -292328537500],   # 2024-01-03·04(파이낸셜뉴스 보도와 같음)
        [46323, -1476, -368294112500], [46324, -324, -72006000000],       # 2026-10-28·29(29일은 장중)
        [None, None, None]]


def test_parse_keeps_dated_rows_and_drops_intraday_today():
    df = F.parse(ROWS, datetime.datetime(2026, 10, 29, 15, 3))
    assert [d.isoformat() for d in df.index.date] == ["2024-01-03", "2024-01-04", "2026-10-28"]
    assert df.loc["2024-01-03", "fgn_net_qty"] == -16516 and df.loc["2024-01-03", "fgn_net_amt"] == -1473856650000
    after = F.parse(ROWS, datetime.datetime(2026, 10, 29, 16, 30))
    assert after.index[-1] == pd.Timestamp("2026-10-29") and after["fgn_net_qty"].iloc[-1] == -324


def test_merge_overwrites_same_dates_only():
    old = F.parse(ROWS[:4], datetime.datetime(2026, 10, 29, 17))
    new = pd.DataFrame({"fgn_net_qty": [-3000.0], "fgn_net_amt": [None]}, index=pd.DatetimeIndex(["2024-01-04"], name="date"))
    df = F.merge(old, new)
    assert df["fgn_net_qty"].tolist() == [-16516, -3000] and list(df.columns) == F.COLS
    assert F.merge(pd.DataFrame(), new).equals(new[F.COLS])


def test_front_code_reads_dat_and_falls_back(tmp_path):
    p = tmp_path / "FT_ITEM_KOR_FUTALL.dat"
    p.write_text("Ver 1.0\n종목코드:t:8@종목명:t:40\n"
                 "A016C00N@코스피200 F 2612 야간@F@01@1@K200 F 2612 야간@0@1@08451545@NDV\n"
                 "A0173000@코스피200 F 2703@F@01@0@K200 F 2703@0@4@08451545@DRV\n"
                 "A016C000@코스피200 F 2612@F@01@1@K200 F 2612@0@1@08451545@DRV\n", encoding="cp949")
    assert F.front_code(p) == "A016C000"
    assert F.front_code(tmp_path / "없음.dat") == F.FALLBACK_CODE


def test_formula_matches_verified_syntax():
    f = F.formula("A016C000", "2010-01-01", "2026-10-29", 6145)
    assert f == ('=_xll.IMDH("FUT","A016C000","일자,외국인합순매수수량,외국인합순매수금액","20100101","20261029",6145,'
                 '"Per=D,Sort=A,DtFmt=1,Pos=20")')


def test_run_skips_when_infomax_off(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "HIST", tmp_path / "foreign_futures_daily.csv")
    monkeypatch.setattr(F.B, "infomax_running", lambda: False)
    assert F.run().startswith("건너뜀") and not F.HIST.exists()
