# -*- coding: utf-8 -*-
"""증시자금 일별 이력 — 금융투자협회 종합통계(FreeSIS, freesis.kofia.or.kr) '증시자금추이'·'신용공여 잔고 추이'.

요청 형식(2026-10-26 사이트 JS 에서 확인 — eXbuilder6 앱, 쿠키·로그인 없이 동작):
  POST https://freesis.kofia.or.kr/meta/getMetaDataList.do   Content-Type: application/json
  {"dmSearch": {"OBJ_NM": "<서비스>BO", "tmpV1": "D", "tmpV45": "YYYYMMDD", "tmpV46": "YYYYMMDD", "tmpV40": "1000000", "tmpV41": "1"}}
  tmpV1 자료주기(D 일), tmpV45~46 조회기간, tmpV40 표시 단위(나누는 수 — 1000000 이면 백만원). tmpV40·tmpV41 이 없으면 금액 열이 모두 null.
  응답 {"ds1": [{"TMPV1": "YYYYMMDD", "TMPV2": .., ...}]} — 최신일부터 내림차순. 1998년부터 한 번에 약 7,200행(1초 안).
  화면 열 이름은 POST /meta/getSrvData.do {"dmSearchData": {"strSvrId": "<서비스 ID>", ...}} 의 dsGrid 로 확인했다.
서비스·열(단위 백만원, 반대매매비중만 %):
  STATSCU0100000060BO 증시자금추이(1998-06-18 부터)
    V2 customer_deposits 투자자예탁금(장내파생상품 거래예수금 제외) · V3 deriv_deposits 장내파생상품 거래예수금
    V4 rp_balance 대고객 RP 매도잔고 · V5 unpaid 위탁매매 미수금 · V6 forced_sales 미수금 대비 실제 반대매매금액
    V7 forced_sales_pct 미수금 대비 반대매매비중(%)
  STATSCU0100000070BO 신용공여 잔고 추이(1998-07-01 부터 유효 — 그 전 행은 0 이라 비운다. 결제일 기준(T+2), 2002-05-02 부터
    유가증권·코스닥 합산, 2007-07-02 부터 ETF 포함)
    V2 credit_balance 신용거래융자 전체 · V3 credit_kospi 유가증권 · V4 credit_kosdaq 코스닥
    V5 short_balance 신용거래대주 전체 · V6 short_kospi · V7 short_kosdaq · V8 ipo_loans 청약자금대출 · V9 securities_loans 예탁증권담보융자
출력: data/history/kofia_free_daily.csv (date + 위 열). 증분 — 마지막 날짜 − REFETCH_DAYS 부터 다시 받아 덮어쓴다(정정 반영).
사용법: python -X utf8 kofia_free.py [--full]
"""
import datetime as dt
import os
import sys
import time

import pandas as pd
import requests

import config

OUT = config.DATA_DIR / "history" / "kofia_free_daily.csv"
URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://freesis.kofia.or.kr/"}
SOURCE = "금융투자협회 종합통계(freesis.kofia.or.kr)"
START = dt.date(1998, 1, 1)
CREDIT_FROM = "1998-07-01"   # 신용공여 유효 시작(사이트 안내) — 그 전의 0 은 값이 아니다
REFETCH_DAYS = 10
DELAY = 1.0
SERVICES = {
    "STATSCU0100000060BO": {"TMPV2": "customer_deposits", "TMPV3": "deriv_deposits", "TMPV4": "rp_balance",
                            "TMPV5": "unpaid", "TMPV6": "forced_sales", "TMPV7": "forced_sales_pct"},
    "STATSCU0100000070BO": {"TMPV2": "credit_balance", "TMPV3": "credit_kospi", "TMPV4": "credit_kosdaq",
                            "TMPV5": "short_balance", "TMPV6": "short_kospi", "TMPV7": "short_kosdaq",
                            "TMPV8": "ipo_loans", "TMPV9": "securities_loans"},
}
CREDIT_COLS = list(SERVICES["STATSCU0100000070BO"].values())
COLUMNS = [c for m in SERVICES.values() for c in m.values()]


def _fetch(obj: str, start: dt.date, end: dt.date) -> list[dict]:
    body = {"dmSearch": {"OBJ_NM": obj, "tmpV1": "D", "tmpV45": f"{start:%Y%m%d}", "tmpV46": f"{end:%Y%m%d}",
                         "tmpV40": "1000000", "tmpV41": "1"}}
    for i in range(4):
        try:
            r = requests.post(URL, json=body, headers=HEADERS, timeout=(20, 120))
            r.raise_for_status()
            rows = r.json()["ds1"]
            break
        except (requests.RequestException, ValueError, KeyError):
            if i == 3:
                raise
            time.sleep(3 * (i + 1))
    time.sleep(DELAY)
    return rows


def parse(rows: list[dict], colmap: dict) -> pd.DataFrame:
    """ds1 행 → 날짜 인덱스(오름차순) DataFrame, 열 이름은 colmap 값. 날짜 없는 행은 뺀다."""
    recs = [{"date": pd.Timestamp(r["TMPV1"]), **{c: r.get(k) for k, c in colmap.items()}} for r in rows if r.get("TMPV1")]
    df = pd.DataFrame(recs, columns=["date", *colmap.values()]).set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df.apply(pd.to_numeric, errors="coerce")


def combine(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """두 서비스를 날짜로 합치고, 신용공여 유효 시작 전 행과 전 열이 0 인 행(값 없음)을 비운다."""
    df = pd.concat(parts, axis=1, sort=True)
    for c in COLUMNS:
        if c not in df:
            df[c] = pd.NA
    df = df[COLUMNS]
    df.loc[df.index < CREDIT_FROM, CREDIT_COLS] = pd.NA
    df.loc[(df[CREDIT_COLS].fillna(0) == 0).all(axis=1), CREDIT_COLS] = pd.NA
    df.index.name = "date"
    return df.dropna(how="all")


def merge(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """새로 받은 날짜는 덮어쓰고(정정 반영), 나머지는 기존 값을 둔다."""
    if old is None or old.empty:
        return new
    return pd.concat([old[~old.index.isin(new.index)], new]).sort_index()


def load() -> pd.DataFrame | None:
    try:
        return pd.read_csv(OUT, index_col=0, parse_dates=True)
    except (OSError, ValueError):
        return None


def run(full: bool = False, today: dt.date | None = None) -> str:
    """refresh_all 단계 — 증분으로 받아 kofia_free_daily.csv 를 갱신한다."""
    today = today or dt.date.today()
    old = None if full else load()
    start = START if old is None or old.empty else max(START, old.index.max().date() - dt.timedelta(days=REFETCH_DAYS))
    new = combine([parse(_fetch(obj, start, today), cols) for obj, cols in SERVICES.items()])
    if new.empty and (old is None or old.empty):
        raise RuntimeError(f"FreeSIS {start}~{today} 0건 — 사이트 변경 또는 일시 차단")
    df = merge(old, new)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".csv.tmp")
    df.to_csv(tmp, date_format="%Y-%m-%d")
    os.replace(tmp, OUT)
    last = df.dropna(subset=["customer_deposits"]).index.max()
    return f"{len(new)}행 받음 · 전체 {len(df)}행 · 마지막 {last:%Y-%m-%d}"


if __name__ == "__main__":
    print(run(full="--full" in sys.argv))
