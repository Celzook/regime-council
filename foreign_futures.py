# -*- coding: utf-8 -*-
"""외국인 KOSPI200 선물 순매수 일별 수집 — 마켓타이밍 수급 신호(timing.py foreign_futures)의 입력. 원천: 인포맥스 엑셀 애드인.

함수   IMDH(과거 시계열) — =_xll.IMDH("FUT", 코드, "일자,외국인합순매수수량,외국인합순매수금액", 시작일, 종료일, 건수,
       "Per=D,Sort=A,DtFmt=1,Pos=20"). 항목 이름은 C:\\Infomax\\bin\\ini\\선물_옵션_투자자_h.dat(투자자·FUT·CALC CODE(2/2)·구간 SUM).
       IMDT(캘린더 등 고정 양식)가 아니라 IMDH 다. 캘린더와 달리 숨김 엑셀에서도 값이 들어온다(2026-10-29 실측, 약 12초).
코드   CODE(2/2) = 코드 2·3번째 글자(상품코드 01 = 코스피200 선물) — 투자자 값은 종목이 아니라 상품 전체(전 월물 합)다.
       만기 지난 A0169000(2609)·원월 A0173000(2703)도 근월 A016C000 과 같은 값을 돌려준다(2026-10-29 실측).
       그래도 근월 코드는 C:\\Infomax\\bin\\code\\FT_ITEM_KOR_FUTALL.dat(cp949, 상품코드 01·연결구분 1·주간 DRV)에서 고르고,
       못 읽으면 FALLBACK_CODE.
단위   수량 = 계약, 금액 = 원(.dat 표시단위 — 옛 .csv 의 '천'은 틀림). 금액 ÷ 수량 ÷ 25만(승수) ≈ KOSPI200 지수로 확인했고,
       2024-01-03 −16,516계약·−1조4,738억원, 01-04 −3,347계약·−2,923억원이 파이낸셜뉴스(2024-01-04) 보도와 같다.
       승수가 2017-03 에 50만 → 25만으로 바뀌어 그 전 계약수는 크기가 다르다(1년 z·5년 백분위 창은 그 뒤라 영향 없음).
이력   2010-01-04 부터 받아진다(4,140일). 처음엔 START 부터 전체, 그 뒤 최근 RECENT_DAYS 만 받아 날짜별로 덮어쓴다.
       오늘 행은 16시(KST, options_pc.CLOSE_HOUR 와 같음) 전이면 장중 누적이라 뺀다.
저장   data/history/foreign_futures_daily.csv — date, fgn_net_qty(계약), fgn_net_amt(원).
실패   인포맥스 프로그램이 꺼져 있으면 건너뛴다(기존 파일 유지). 엑셀 COM 은 infomax_bridge.LOCK 으로 한 번에 하나.

사용법: python foreign_futures.py [--full]   # Windows 파이썬(python.exe -X utf8) — 인포맥스 로그인 필요
"""
import datetime
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

import config
import infomax_bridge as B

HIST = config.DATA_DIR / "history" / "foreign_futures_daily.csv"
CODE_FILE = Path(r"C:\Infomax\bin\code\FT_ITEM_KOR_FUTALL.dat")
FALLBACK_CODE = "A016C000"
ITEMS = ("외국인합순매수수량", "외국인합순매수금액")
COLS = ["fgn_net_qty", "fgn_net_amt"]
START = "2010-01-01"
RECENT_DAYS = 60
CLOSE_HOUR = 16
SHEET = "이력"


def front_code(path: Path = CODE_FILE) -> str:
    """코스피200 선물 근월 코드(상품코드 01·연결구분 1·주간 DRV). 파일을 못 읽으면 FALLBACK_CODE."""
    try:
        for line in path.read_text(encoding="cp949", errors="replace").splitlines():
            f = line.split("@")
            if len(f) >= 10 and f[3] == "01" and f[4] == "1" and f[9].strip() == "DRV":
                return f[0]
    except OSError:
        pass
    return FALLBACK_CODE


def formula(code: str, start: str, end: str, n: int) -> str:
    return (f'=_xll.IMDH("FUT","{code}","일자,{",".join(ITEMS)}","{start.replace("-", "")}","{end.replace("-", "")}",'
            f'{n},"Per=D,Sort=A,DtFmt=1,Pos=20")')


def parse(rows: list, now: datetime.datetime | None = None) -> pd.DataFrame:
    """숨김 엑셀이 읽은 시트 값 → 날짜 인덱스 표. 첫 칸이 날짜(엑셀 일련번호)인 줄만 쓴다(머리·'조회[…]'·빈 줄 제외).
    오늘 행은 16시 전이면 장중 누적이라 뺀다. 순수 함수 — 테스트는 합성 줄로 부른다."""
    now = now or datetime.datetime.now()
    recs = {}
    for r in rows or []:
        r = list(r) + [None] * 3
        if not isinstance(r[0], (int, float)) or isinstance(r[0], bool):
            continue
        d = B._xl_date(r[0])
        q, a = r[1], r[2]
        if not d or not isinstance(q, (int, float)) or isinstance(q, bool):
            continue
        recs[d] = (float(q), float(a) if isinstance(a, (int, float)) and not isinstance(a, bool) else None)
    df = pd.DataFrame.from_dict(recs, orient="index", columns=COLS)
    df.index = pd.DatetimeIndex(df.index, name="date")
    df = df.sort_index()
    if len(df) and now.hour < CLOSE_HOUR:
        df = df[df.index.date != now.date()]
    if len(df) and df.index[-1].date() > now.date():
        raise ValueError(f"거래일 {df.index[-1].date()} 이 오늘보다 뒤")
    return df


def merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """새로 받은 날짜는 덮어쓰고 나머지는 그대로(여러 번 돌려도 같은 결과)."""
    df = pd.concat([old[~old.index.isin(new.index)], new]).sort_index() if len(old) else new
    df.index.name = "date"
    return df[COLS]


def _save(df: pd.DataFrame) -> None:
    HIST.parent.mkdir(parents=True, exist_ok=True)
    tmp = HIST.with_suffix(".csv.tmp")
    df.to_csv(tmp, encoding="utf-8")
    os.replace(tmp, HIST)


def _pull(code: str, start: str, end: str) -> dict:
    """IMDH 한 칸짜리 통합문서를 임시 폴더(Windows 경로 — 저장소 밖)에 만들어 숨김 엑셀로 읽는다."""
    from openpyxl import Workbook
    n = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 10   # 건수 상한 — 달력일이면 충분
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws["A1"] = formula(code, start, end, n)
    path = Path(tempfile.gettempdir()) / "인포맥스_선물투자자.xlsx"
    wb.save(path)
    try:
        return B.read_headless(path, sheets=(SHEET,), watch=SHEET, wait=60, stable=4) or {}
    finally:
        path.unlink(missing_ok=True)


def run(full: bool = False, now: datetime.datetime | None = None) -> str:
    """refresh_all 한 단계. 인포맥스가 꺼져 있거나 다른 읽기가 진행 중이면 건너뛴다(기존 파일 유지)."""
    now = now or datetime.datetime.now()
    if not B.XLL.exists() or not B.infomax_running():
        return "건너뜀(인포맥스 꺼짐 — 기존 이력 유지)"
    if not B.LOCK.acquire(blocking=False):
        return "건너뜀(인포맥스 읽기 진행 중)"
    try:
        old = pd.read_csv(HIST, index_col=0, parse_dates=True) if HIST.exists() and not full else pd.DataFrame()
        start = (now.date() - datetime.timedelta(days=RECENT_DAYS)).isoformat() if len(old) else START
        code = front_code()
        raw = _pull(code, start, now.date().isoformat())
    finally:
        B.LOCK.release()
    if not raw.get("open"):
        raise RuntimeError(f"인포맥스 숨김 엑셀 읽기 실패({raw.get('reason')})")
    new = parse((raw.get("sheets") or {}).get(SHEET), now)
    if new.empty:
        raise RuntimeError(f"IMDH {code} 외국인합순매수수량 값 없음({start}~)")
    df = merge(old, new)
    _save(df)
    last = df.iloc[-1]
    return f"{df.index[-1].date()} 외국인 선물 {int(last['fgn_net_qty']):+,}계약 · {len(df)}일({df.index[0].date()}~)"


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(run(full="--full" in sys.argv))
