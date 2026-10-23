# -*- coding: utf-8 -*-
"""과거 시계열 수집 → data/history/*.csv  (과거 사례 점검용)

출처는 Refinitiv(LSEG) Workspace 가 기본이다(refinitiv_src). 이 계정에 권한이 없는 시리즈만 예외로 남긴다.
- kofia_daily.csv: 금융투자협회 최종호가수익률(15:30) — Refinitiv 'KOFIA OTC Last Quote Yield' RIC(=KQ).
  국고 2/3/5/10년, 회사채 AA- 3년. 원 사이트(kofiabond.or.kr)와 같은 값이다.
- yf_daily.csv(파일명은 호환 때문에 유지): 환율·지수·원자재·미국채 10년. VIX 는 권한이 없어 yfinance.
- fred_daily.csv: 미국채 2·10년·10년 BEI(명목−TIPS)·연준 목표금리(상단)는 Refinitiv,
  ICE BofA IG·HY OAS·유효수익률은 권한이 없어 FRED(키 불필요 CSV). FRED 는 ICE 시리즈를 최근 3년만 줘서
  기존 CSV 와 병합해 받은 날부터 누적 보존한다.
갱신: 받은 열은 열 전체를 새 값으로 바꾸고(출처가 한 열 안에서 섞이지 않게), 이번에 실패한 열만 기존 CSV 값을 남긴다.
"""
import datetime
import subprocess
import sys
from pathlib import Path

import config
import refinitiv_src as R

HIST_DIR = config.DATA_DIR / "history"
# (RIC, 열 이름) — KOFIA 최종호가수익률. 2년물은 2021-03-10 부터.
KOFIA_RICS = [("KRTSY2Y=KQ", "ktb_2y"), ("KRTSY3Y=KQ", "ktb_3y"), ("KRTSY5Y=KQ", "ktb_5y"),
              ("KRTSY10Y=KQ", "ktb_10y"), ("KRGUCORP=KQ", "corp_aa_minus_3y"),
              ("KRCORP=KQ", "corp_bbb_minus_3y"), ("KRCD=KQ", "cd_91d"), ("KRBASE=BOKK", "bok_base")]
KOFIA_START = datetime.date(2012, 1, 1)
LONG_START = datetime.date(1980, 1, 1)

# yfinance 표기 티커 → 열 이름. RIC 변환은 refinitiv_src.TICKER_RIC, ^VIX 만 yfinance(YF_ONLY).
YF_TICKERS = {"KRW=X": "usdkrw", "^KS11": "kospi", "^GSPC": "sp500",
              "^VIX": "vix", "DX-Y.NYB": "dxy", "^TNX": "ust_10y",
              "^KQ11": "kosdaq", "^IXIC": "nasdaq", "^N225": "nikkei", "^STOXX50E": "stoxx50",
              "EEM": "eem", "JPY=X": "usdjpy", "EURUSD=X": "eurusd", "CNY=X": "usdcny",
              "CL=F": "wti", "GC=F": "gold",
              "^KS200": "kospi200", "KSc1": "ks200_fut"}   # KSc1 = KOSPI200 선물 최근월 연결(만기마다 다음 월물로 넘어감)

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_START = datetime.date(1990, 1, 1)
# (출처, id, 열 이름, 단위, 설명) — OAS 는 FRED 원값이 %p 라 열에는 그대로 두고 분석 단계에서 bp 로 바꾼다
US_SERIES = [
    ("refinitiv", "US2YT=RR", "ust_2y", "%", "미국채 2년(벤치마크)"),
    ("refinitiv", "US10YT=RR", "ust_10y_cmt", "%", "미국채 10년(벤치마크)"),
    ("refinitiv", "USFFTARGET=", "fed_funds", "%", "연준 목표금리(범위 상단)"),
    ("refinitiv", "US10YT=RR - US10YTIP=RR", "be_10y", "%", "미국 10년 BEI(명목−TIPS)"),
    ("fred", "BAMLC0A0CM", "us_ig_oas", "%p", "ICE BofA 미국 IG OAS"),
    ("fred", "BAMLH0A0HYM2", "us_hy_oas", "%p", "ICE BofA 미국 HY OAS"),
    ("fred", "BAMLC0A0CMEY", "us_ig_yield", "%", "ICE BofA 미국 IG 유효수익률"),
    ("fred", "BAMLH0A0HYM2EY", "us_hy_yield", "%", "ICE BofA 미국 HY 유효수익률"),
]
FRED_SERIES = [(sid, key, unit, name) for src, sid, key, unit, name in US_SERIES if src == "fred"]


def _merge_cols(new, path: Path):
    """받은 열은 통째로 새 값, 못 받은 열은 기존 CSV 값 유지."""
    import pandas as pd

    if path.exists():
        old = pd.read_csv(path, index_col=0, parse_dates=True)
        for col in old.columns:
            if col not in new.columns or new[col].dropna().empty:
                new = new.drop(columns=[col], errors="ignore").join(old[[col]], how="outer")
    new = new[~new.index.duplicated(keep="last")].sort_index()
    new.index.name = "date"
    return new


def _ref_series(ric_expr: str, start: datetime.date):
    """'A - B'(공백 포함) 는 두 RIC 의 차(공통 날짜). RIC 자체에 '-' 가 들어가는 경우(BBB-…)가 있어 공백으로 가른다."""
    if " - " in ric_expr:
        a, b = ric_expr.split(" - ")
        sa, sb = _ref_series(a, start), _ref_series(b, start)
        return (sa - sb).dropna()
    return R.history(ric_expr, start, adjusted=False)


def fetch_kofia_history() -> Path:
    import pandas as pd

    HIST_DIR.mkdir(parents=True, exist_ok=True)
    path = HIST_DIR / "kofia_daily.csv"
    cols = {}
    for ric, key in KOFIA_RICS:
        try:
            s = _ref_series(ric, KOFIA_START)
            cols[key] = s
            print(f"  {key}({ric}): {len(s)}일 ({s.index.min().date()} ~ {s.index.max().date()})")
        except Exception as e:  # noqa: BLE001
            print(f"  경고: {key}({ric}) 실패: {e}")
    df = _merge_cols(pd.DataFrame(cols), path)
    df = df[df.index >= pd.Timestamp(KOFIA_START)]
    df.to_csv(path, encoding="utf-8")
    print(f"  저장: {path} ({len(df)}일, {df.index.min().date() if len(df) else '-'} ~ {df.index.max().date() if len(df) else '-'})")
    return path


def _yf_close(ticker: str):
    import pandas as pd
    import yfinance as yf

    h = yf.Ticker(ticker).history(period="max", auto_adjust=False)
    if h.empty:
        raise ValueError("빈 응답")
    s = h["Close"]
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s


def fetch_yf_history() -> Path | None:
    import pandas as pd

    HIST_DIR.mkdir(parents=True, exist_ok=True)
    path = HIST_DIR / "yf_daily.csv"
    cols = {}
    for ticker, key in YF_TICKERS.items():
        try:
            s = _yf_close(ticker) if ticker in R.YF_ONLY else R.history(ticker, LONG_START, adjusted=False)
            if s.empty:
                raise ValueError("빈 응답")
            cols[key] = s
            print(f"  {key}({R.source_label(ticker)}): {len(s)}일 ({s.index.min().date()} ~ {s.index.max().date()})")
        except Exception as e:  # noqa: BLE001
            print(f"  경고: {key}({ticker}) 실패: {e}")
    if not cols:
        return None
    df = _merge_cols(pd.DataFrame(cols), path)
    missing = [k for k in YF_TICKERS.values() if k not in df.columns]
    if missing:
        print(f"  경고: 필수 열 누락 {missing} — 주식·환 유사국면 계산에 영향")
    df.to_csv(path, encoding="utf-8")
    print(f"  저장: {path}")
    return path


def fetch_fred_series(series_id: str, start: datetime.date = FRED_START):
    """FRED 그래프 CSV(키 불필요) → pd.Series. 휴일 결측('.'·빈칸)은 NaN 후 제거."""
    import io

    import pandas as pd

    # 브라우저 UA 를 붙이면 FRED 가 연결을 끊는다(봇 탐지) — curl 기본 UA 로 호출
    r = subprocess.run(
        ["curl", "-s", "-S", "-m", "90", "-w", "\n%{http_code}",
         f"{FRED_URL}?id={series_id}&cosd={start.isoformat()}"],
        capture_output=True, text=True, timeout=120,
    )
    body, _, code = r.stdout.rpartition("\n")
    if r.returncode != 0 or code.strip() != "200":
        raise RuntimeError(f"FRED {series_id} 호출 실패: http={code.strip()} {r.stderr[:200]}")
    df = pd.read_csv(io.StringIO(body))
    if df.shape[1] < 2:
        raise RuntimeError(f"FRED {series_id} 응답 형식 이상: {body[:120]!r}")
    s = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s.index = pd.to_datetime(df.iloc[:, 0])
    return s.dropna()


def fetch_fred_history() -> Path | None:
    """미국 금리·크레딧 → fred_daily.csv (파일명은 호환 때문에 유지)."""
    import pandas as pd

    HIST_DIR.mkdir(parents=True, exist_ok=True)
    path = HIST_DIR / "fred_daily.csv"
    cols = {}
    for src, sid, key, _unit, name in US_SERIES:
        try:
            s = _ref_series(sid, FRED_START) if src == "refinitiv" else fetch_fred_series(sid)
            cols[key] = s
            print(f"  {key}({src} {sid}): {len(s)}일 ({s.index.min().date()} ~ {s.index.max().date()})")
        except Exception as e:  # noqa: BLE001
            print(f"  경고: {name}({sid}) 실패: {e}")
    if not cols:
        return None
    new = pd.DataFrame(cols)
    if path.exists():
        # FRED ICE 시리즈는 최근 3년만 온다 — 창 밖으로 밀려난 과거값은 기존 CSV 에서 보존
        old = pd.read_csv(path, index_col=0, parse_dates=True)
        fred_cols = [k for src, _, k, _, _ in US_SERIES if src == "fred" and k in old.columns]
        new = new.combine_first(old[fred_cols]) if fred_cols else new
    df = _merge_cols(new, path)
    df.to_csv(path, encoding="utf-8")
    print(f"  저장: {path}")
    return path


def main():
    print("과거 시계열 수집 중... (Refinitiv)")
    if not R.available(force=True):
        print(f"  Refinitiv 미연결 — {R.status()['error']}. 기존 CSV 를 그대로 둔다.")
        return
    try:
        fetch_kofia_history()
    except Exception as e:  # noqa: BLE001
        print(f"  KOFIA 수집 실패: {e}")
    fetch_yf_history()
    fetch_fred_history()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
