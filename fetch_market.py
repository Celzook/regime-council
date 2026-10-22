# -*- coding: utf-8 -*-
"""시장 스냅샷 생성 → data/market.json

- 국고·회사채 금리: 금융투자협회 채권정보센터 최종호가수익률 (오후 15:30 확정치).
  Outlook Hub 에서 검증된 호출 방식 재사용 (큰 응답에서 urllib 이 끊긴 전례가 있어 curl 사용).
- 환율·지수: yfinance 설치 시 자동 수집, 없으면 생략.
- 수동 입력: data/manual_inputs.json 이 있으면 "manual" 섹션으로 병합
  (스왑포인트, 헤지비용 등 자동화 어려운 항목용. 각 항목에 value/unit/as_of/source 필수).

모든 수치는 {value, unit, as_of, source} 묶음으로 저장한다. 원본 응답은 data/_raw/ 에 보존.
"""
import datetime
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

import config

KOFIA_URL = "https://www.kofiabond.or.kr/proframeWeb/XMLSERVICES/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REF = ("https://www.kofiabond.or.kr/websquare/websquare.html"
       "?w2xPath=/xml/bondint/lastrop/BISLastAskPrcTrm.xml")

# 최종호가수익률 종목코드 (요청 순서가 응답 열 순서)
KOFIA_CODES = [
    ("3019", "ktb_2y", "국고채 2년"),
    ("3000", "ktb_3y", "국고채 3년"),
    ("3007", "ktb_5y", "국고채 5년"),
    ("3013", "ktb_10y", "국고채 10년"),
    ("3009", "corp_aa_minus_3y", "회사채 AA- 3년"),
]

YF_TICKERS = {
    "fx": [("KRW=X", "usdkrw", "KRW", "USD/KRW"),
           ("DX-Y.NYB", "dollar_index", "pt", "달러인덱스(ICE)"),
           ("JPY=X", "usdjpy", "JPY", "USD/JPY"),
           ("EURUSD=X", "eurusd", "USD", "EUR/USD"),
           ("CNY=X", "usdcny", "CNY", "USD/CNY")],
    "equity": [("^KS11", "kospi", "pt", "KOSPI"),
               ("^KQ11", "kosdaq", "pt", "KOSDAQ"),
               ("^GSPC", "sp500", "pt", "S&P500"),
               ("^IXIC", "nasdaq", "pt", "나스닥 종합"),
               ("^N225", "nikkei", "pt", "닛케이225"),
               ("^STOXX50E", "stoxx50", "pt", "유로스톡스50"),
               ("EEM", "eem", "USD", "iShares MSCI EM ETF — 신흥국 지수 대용"),
               ("^VIX", "vix", "pt", "VIX")],
    "commodities": [("CL=F", "wti", "USD/bbl", "WTI 근월물"),
                    ("GC=F", "gold", "USD/oz", "금 근월물")],
}

# FRED 스냅샷: (FRED id, 섹션, 키, 단위, bp 환산 여부) — 이름은 fetch_history.FRED_SERIES 와 같다
FRED_SNAPSHOT = [
    ("DGS2", "rates_us", "ust_2y", "%", False),
    ("DGS10", "rates_us", "ust_10y", "%", False),
    ("DFF", "rates_us", "fed_funds", "%", False),
    ("T10YIE", "rates_us", "breakeven_10y", "%", False),
    ("BAMLC0A0CM", "credit_us", "ig_oas_bp", "bp", True),
    ("BAMLH0A0HYM2", "credit_us", "hy_oas_bp", "bp", True),
    ("BAMLC0A0CMEY", "credit_us", "ig_yield", "%", False),
    ("BAMLH0A0HYM2EY", "credit_us", "hy_yield", "%", False),
]


def _field(value, unit, as_of, source):
    return {"value": value, "unit": unit, "as_of": as_of, "source": source}


def fetch_kofia() -> dict:
    """최종호가수익률(listTrm, 15:30) 최근 영업일 값."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=14)
    vals = [("val1", "DD"), ("val2", start.strftime("%Y%m%d")),
            ("val3", end.strftime("%Y%m%d")), ("val4", "1530")]
    vals += [(f"val{i + 5}", code) for i, (code, _, _) in enumerate(KOFIA_CODES)]
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in vals)
    body = (
        '<?xml version="1.0" encoding="utf-8"?><message><proframeHeader>'
        "<pfmAppName>BIS-KOFIABOND</pfmAppName>"
        "<pfmSvcName>BISLastAskPrcROPSrchSO</pfmSvcName>"
        "<pfmFnName>listTrm</pfmFnName></proframeHeader><systemHeader></systemHeader>"
        f"<BISComDspDatDTO>{inner}</BISComDspDatDTO></message>"
    )
    raw_dir = config.DATA_DIR / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    resp_path = raw_dir / f"kofia_{end.isoformat()}.xml"
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                     encoding="utf-8") as f:
        f.write(body)
        req_path = f.name
    r = subprocess.run(
        ["curl", "-s", "-S", "-A", UA, "-e", REF,
         "-H", "Content-Type: application/xml; charset=utf-8",
         "--data-binary", f"@{req_path}", "-o", str(resp_path),
         "-w", "%{http_code}", KOFIA_URL],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0 or r.stdout.strip() != "200":
        raise RuntimeError(f"KOFIA 호출 실패: rc={r.returncode} http={r.stdout} {r.stderr[:200]}")
    text = resp_path.read_text(encoding="utf-8", errors="replace")

    rows = []
    for m in re.finditer(r"<BISComDspDatDTO>(.*?)</BISComDspDatDTO>", text, re.S):
        d = dict(re.findall(r"<(val\d+)>(.*?)</\1>", m.group(1), re.S))
        rows.append(d)
    if not rows:
        raise RuntimeError("KOFIA 응답에 데이터 행이 없습니다 (휴장일이거나 형식 변경).")

    def row_date(d):
        raw = re.sub(r"\D", "", d.get("val1", ""))
        return raw if len(raw) == 8 else ""

    rows = [d for d in rows if row_date(d)]
    rows.sort(key=row_date)
    latest = rows[-1]
    as_of = datetime.datetime.strptime(row_date(latest), "%Y%m%d").date().isoformat()

    out = {}
    for i, (_, key, name) in enumerate(KOFIA_CODES):
        v = latest.get(f"val{i + 2}", "").strip()
        try:
            out[key] = _field(float(v), "%", as_of,
                              f"금융투자협회 채권정보센터 최종호가수익률 15:30 ({name})")
        except ValueError:
            print(f"  경고: {name} 값 파싱 실패('{v}') — 생략")
    if "ktb_3y" in out and "ktb_10y" in out:
        out["curve_3s10s_bp"] = _field(
            round((out["ktb_10y"]["value"] - out["ktb_3y"]["value"]) * 100, 1),
            "bp", as_of, "계산(ktb_10y - ktb_3y)")
    if "ktb_3y" in out and "corp_aa_minus_3y" in out:
        out["spread_aa_minus_bp"] = _field(
            round((out["corp_aa_minus_3y"]["value"] - out["ktb_3y"]["value"]) * 100, 1),
            "bp", as_of, "계산(corp_aa_minus_3y - ktb_3y)")
    return out


def fetch_yfinance() -> dict:
    try:
        import yfinance as yf
    except ImportError:
        print("  yfinance 미설치 — 환율·지수 자동 수집 생략 (pip install yfinance)")
        return {}
    out = {section: {} for section in YF_TICKERS}
    for section, items in YF_TICKERS.items():
        for ticker, key, unit, name in items:
            try:
                h = yf.Ticker(ticker).history(period="5d")
                if h.empty:
                    raise ValueError("빈 응답")
                last = h.iloc[-1]
                close = float(last["Close"])
                if not math.isfinite(close):
                    raise ValueError("종가가 NaN")
                as_of = h.index[-1].date().isoformat()
                out[section][key] = _field(
                    round(close, 4 if abs(close) < 10 else 2), unit, as_of,  # EUR/USD·USD/CNY 는 소수 4자리
                    f"yfinance {ticker} 종가 ({name})")
            except Exception as e:  # noqa: BLE001 — 개별 티커 실패는 건너뜀
                print(f"  경고: {name}({ticker}) 수집 실패: {e}")
    return out


def fetch_fred() -> dict:
    """미국 금리·크레딧 최신값. FRED 는 1영업일 늦게 반영된다(as_of 로 표시)."""
    from fetch_history import FRED_SERIES, fetch_fred_series

    names = {sid: name for sid, _, _, name in FRED_SERIES}
    start = datetime.date.today() - datetime.timedelta(days=30)
    out = {"rates_us": {}, "credit_us": {}}
    for sid, section, key, unit, to_bp in FRED_SNAPSHOT:
        try:
            s = fetch_fred_series(sid, start)
            if s.empty:
                raise ValueError("최근 30일 값 없음")
            v = float(s.iloc[-1]) * (100 if to_bp else 1)
            out[section][key] = _field(round(v, 1 if to_bp else 3), unit, s.index[-1].date().isoformat(),
                                       f"FRED {sid} ({names[sid]})" + (" ×100 bp 환산" if to_bp else ""))
        except Exception as e:  # noqa: BLE001 — 개별 시리즈 실패는 건너뜀
            print(f"  경고: FRED {sid} 수집 실패: {e}")
    r = out["rates_us"]
    if "ust_2y" in r and "ust_10y" in r:
        r["ust_2s10s_bp"] = _field(round((r["ust_10y"]["value"] - r["ust_2y"]["value"]) * 100, 1), "bp",
                                   r["ust_10y"]["as_of"], "계산(ust_10y - ust_2y)")
    return out


def main():
    print("시장 스냅샷 생성 중...")
    snapshot = {
        "meta": {
            "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": "모든 수치는 value/unit/as_of/source 묶음. 에이전트는 이 파일의 수치만 인용한다.",
        },
        "rates_kr": {},
        "rates_us": {},
        "credit_us": {},
        "fx": {},
        "equity": {},
        "commodities": {},
    }

    try:
        snapshot["rates_kr"] = fetch_kofia()
        print(f"  KOFIA 금리 {len([k for k in snapshot['rates_kr'] if not k.endswith('_bp')])}종 수집 완료")
    except Exception as e:  # noqa: BLE001
        print(f"  경고: KOFIA 수집 실패 — {e}")

    yf_data = fetch_yfinance()
    for section in YF_TICKERS:
        snapshot[section].update(yf_data.get(section, {}))

    fred = fetch_fred()
    for section, fields in fred.items():
        snapshot[section].update(fields)
    print(f"  FRED 미국 금리·크레딧 {sum(len(v) for v in fred.values())}종 수집 완료")
    k10, u10 = snapshot["rates_kr"].get("ktb_10y"), snapshot["rates_us"].get("ust_10y")
    if k10 and u10:
        snapshot["rates_us"]["kr_minus_us_10y_bp"] = _field(
            round((k10["value"] - u10["value"]) * 100, 1), "bp", min(k10["as_of"], u10["as_of"]),
            f"계산(rates_kr.ktb_10y {k10['as_of']} - rates_us.ust_10y {u10['as_of']})")

    manual_path = config.DATA_DIR / "manual_inputs.json"
    if manual_path.exists():
        manual = json.loads(manual_path.read_text(encoding="utf-8"))
        bad = [k for k, v in manual.items()
               if not isinstance(v, dict) or "as_of" not in v or "source" not in v]
        for k in bad:
            print(f"  경고: 수동 입력 '{k}' 에 as_of/source 누락 — 제외")
            manual.pop(k)
        snapshot["manual"] = manual
        print(f"  수동 입력 {len(manual)}건 병합")

    # 신선도 표시 (단순 일수 기준)
    stale = []
    today = datetime.date.today()
    for section, fields in snapshot.items():
        if section == "meta" or not isinstance(fields, dict):
            continue
        for key, f in fields.items():
            if isinstance(f, dict) and "as_of" in f:
                try:
                    age = (today - datetime.date.fromisoformat(f["as_of"])).days
                    if age > 5:
                        stale.append(f"{section}.{key}({age}일)")
                except ValueError:
                    pass
    snapshot["meta"]["stale_fields"] = stale
    if stale:
        print(f"  신선도 주의: {', '.join(stale)}")

    config.DATA_DIR.mkdir(exist_ok=True)
    out_path = config.DATA_DIR / "market.json"
    # 원자적 교체 — 쓰는 도중 대시보드가 반쯤 쓰인 파일을 읽지 않게.
    # allow_nan=False — NaN 이 섞이면 저장 단계에서 바로 실패시킨다 (JSON 표준 위반 방지).
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8")
    os.replace(tmp_path, out_path)
    print(f"저장: {out_path}")


if __name__ == "__main__":
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
