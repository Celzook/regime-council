# -*- coding: utf-8 -*-
"""과거 시계열 수집 → data/history/*.csv  (과거 사례 점검용)

- KOFIA 최종호가수익률(15:30): 국고 2/3/5/10년, 회사채 AA- 3년. 연 단위로 나눠 호출(응답 크기 제한 회피).
- yfinance: USD/KRW, KOSPI, S&P500, VIX, DXY, 미국채 10년(^TNX) + 시장 전반 분석용
  (KOSDAQ·나스닥·닛케이·유로스톡스50·EEM, 엔·유로·위안, WTI·금). 가능한 최대 기간.
- FRED(키 불필요 CSV): 미국채 2·10년, 연방기금 실효금리, 10년 BEI, 미국 IG·HY OAS·유효수익률.
  ICE BofA 시리즈는 FRED 가 최근 3년만 제공 — 기존 CSV 와 병합해 받은 날부터 누적 보존한다.
증분 갱신: 기존 CSV 가 있으면 마지막 날짜 이후만 받아 덧붙인다(FRED·yfinance 는 전체를 받아 병합).
"""
import datetime
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import config

HIST_DIR = config.DATA_DIR / "history"
KOFIA_URL = "https://www.kofiabond.or.kr/proframeWeb/XMLSERVICES/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REF = ("https://www.kofiabond.or.kr/websquare/websquare.html"
       "?w2xPath=/xml/bondint/lastrop/BISLastAskPrcTrm.xml")
KOFIA_CODES = [("3019", "ktb_2y"), ("3000", "ktb_3y"), ("3007", "ktb_5y"),
               ("3013", "ktb_10y"), ("3009", "corp_aa_minus_3y")]
KOFIA_START = datetime.date(2012, 1, 1)

YF_TICKERS = {"KRW=X": "usdkrw", "^KS11": "kospi", "^GSPC": "sp500",
              "^VIX": "vix", "DX-Y.NYB": "dxy", "^TNX": "ust_10y",
              "^KQ11": "kosdaq", "^IXIC": "nasdaq", "^N225": "nikkei", "^STOXX50E": "stoxx50",
              "EEM": "eem", "JPY=X": "usdjpy", "EURUSD=X": "eurusd", "CNY=X": "usdcny",
              "CL=F": "wti", "GC=F": "gold"}

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_START = datetime.date(1990, 1, 1)
# (FRED id, 열 이름, 단위, 설명) — OAS 는 FRED 원값이 %p 라 열에는 그대로 두고 분석 단계에서 bp 로 바꾼다
FRED_SERIES = [
    ("DGS2", "ust_2y", "%", "미국채 2년(CMT)"),
    ("DGS10", "ust_10y_cmt", "%", "미국채 10년(CMT)"),
    ("DFF", "fed_funds", "%", "연방기금 실효금리"),
    ("T10YIE", "be_10y", "%", "미국 10년 기대인플레이션(BEI)"),
    ("BAMLC0A0CM", "us_ig_oas", "%p", "ICE BofA 미국 IG OAS"),
    ("BAMLH0A0HYM2", "us_hy_oas", "%p", "ICE BofA 미국 HY OAS"),
    ("BAMLC0A0CMEY", "us_ig_yield", "%", "ICE BofA 미국 IG 유효수익률"),
    ("BAMLH0A0HYM2EY", "us_hy_yield", "%", "ICE BofA 미국 HY 유효수익률"),
]


def _kofia_range(start: datetime.date, end: datetime.date) -> dict[str, dict[str, float]]:
    """{날짜: {key: value}} — 요청 순서가 응답 열 순서(val2..)."""
    vals = [("val1", "DD"), ("val2", start.strftime("%Y%m%d")),
            ("val3", end.strftime("%Y%m%d")), ("val4", "1530")]
    vals += [(f"val{i + 5}", code) for i, (code, _) in enumerate(KOFIA_CODES)]
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in vals)
    body = (
        '<?xml version="1.0" encoding="utf-8"?><message><proframeHeader>'
        "<pfmAppName>BIS-KOFIABOND</pfmAppName>"
        "<pfmSvcName>BISLastAskPrcROPSrchSO</pfmSvcName>"
        "<pfmFnName>listTrm</pfmFnName></proframeHeader><systemHeader></systemHeader>"
        f"<BISComDspDatDTO>{inner}</BISComDspDatDTO></message>"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(body)
        req_path = f.name
    resp_path = req_path + ".resp"
    r = subprocess.run(
        ["curl", "-s", "-S", "-A", UA, "-e", REF,
         "-H", "Content-Type: application/xml; charset=utf-8",
         "--data-binary", f"@{req_path}", "-o", resp_path, "-w", "%{http_code}", KOFIA_URL],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0 or r.stdout.strip() != "200":
        raise RuntimeError(f"KOFIA 호출 실패 {start}~{end}: http={r.stdout} {r.stderr[:200]}")
    text = Path(resp_path).read_text(encoding="utf-8", errors="replace")
    out = {}
    for m in re.finditer(r"<BISComDspDatDTO>(.*?)</BISComDspDatDTO>", text, re.S):
        d = dict(re.findall(r"<(val\d+)>(.*?)</\1>", m.group(1), re.S))
        raw = re.sub(r"\D", "", d.get("val1", ""))
        if len(raw) != 8:
            continue
        day = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        row = {}
        for i, (_, key) in enumerate(KOFIA_CODES):
            try:
                row[key] = float(d.get(f"val{i + 2}", "").strip())
            except ValueError:
                row[key] = math.nan
        out[day] = row
    return out


def fetch_kofia_history() -> Path:
    import pandas as pd

    HIST_DIR.mkdir(parents=True, exist_ok=True)
    path = HIST_DIR / "kofia_daily.csv"
    existing = pd.read_csv(path, index_col=0, parse_dates=True) if path.exists() else None
    start = (existing.index.max().date() + datetime.timedelta(days=1)) if existing is not None and len(existing) else KOFIA_START
    today = datetime.date.today()
    rows = {}
    cur = start
    while cur <= today:
        chunk_end = min(datetime.date(cur.year, 12, 31), today)
        print(f"  KOFIA {cur} ~ {chunk_end} ...", end=" ", flush=True)
        try:
            got = _kofia_range(cur, chunk_end)
            print(f"{len(got)}일")
            rows.update(got)
        except Exception as e:  # noqa: BLE001
            print(f"실패: {e}")
        cur = chunk_end + datetime.timedelta(days=1)
    new = pd.DataFrame.from_dict(rows, orient="index")
    if len(new):
        new.index = pd.to_datetime(new.index)
        new = new.sort_index()
    df = pd.concat([existing, new]) if existing is not None else new
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "date"
    df.to_csv(path, encoding="utf-8")
    print(f"  저장: {path} ({len(df)}일, {df.index.min().date() if len(df) else '-'} ~ {df.index.max().date() if len(df) else '-'})")
    return path


def fetch_yf_history() -> Path | None:
    try:
        import pandas as pd
        import yfinance as yf
    except ImportError:
        print("  yfinance 미설치 — 건너뜀")
        return None
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    path = HIST_DIR / "yf_daily.csv"
    frames = []
    for ticker, key in YF_TICKERS.items():
        try:
            h = yf.Ticker(ticker).history(period="max", auto_adjust=False)
            if h.empty:
                raise ValueError("빈 응답")
            s = h["Close"].rename(key)
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            frames.append(s)
            print(f"  {key}: {len(s)}일 ({s.index.min().date()} ~ {s.index.max().date()})")
        except Exception as e:  # noqa: BLE001
            print(f"  경고: {key}({ticker}) 실패: {e}")
    if not frames:
        return None
    df = pd.concat(frames, axis=1, sort=True)
    df = df[~df.index.duplicated(keep="last")]
    if path.exists():
        # 이번에 실패한 티커의 과거 열이 지워지지 않게 기존 CSV 와 열 단위로 병합
        old = pd.read_csv(path, index_col=0, parse_dates=True)
        df = df.combine_first(old)
    missing = [k for k in YF_TICKERS.values() if k not in df.columns]
    if missing:
        print(f"  경고: 필수 열 누락 {missing} — 주식·환 유사국면 계산에 영향")
    df.index.name = "date"
    df.sort_index().to_csv(path, encoding="utf-8")
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
    import pandas as pd

    HIST_DIR.mkdir(parents=True, exist_ok=True)
    path = HIST_DIR / "fred_daily.csv"
    frames = []
    for sid, key, _unit, name in FRED_SERIES:
        try:
            s = fetch_fred_series(sid).rename(key)
            frames.append(s)
            print(f"  {key}: {len(s)}일 ({s.index.min().date()} ~ {s.index.max().date()})")
        except Exception as e:  # noqa: BLE001
            print(f"  경고: {name}({sid}) 실패: {e}")
    if not frames:
        return None
    df = pd.concat(frames, axis=1, sort=True)
    if path.exists():
        # 새 값 우선(수정치 반영), 3년 창 밖으로 밀려난 과거값은 기존 CSV 에서 보존
        old = pd.read_csv(path, index_col=0, parse_dates=True)
        df = df.combine_first(old)
    df.index.name = "date"
    df.sort_index().to_csv(path, encoding="utf-8")
    print(f"  저장: {path}")
    return path


def main():
    print("과거 시계열 수집 중...")
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
