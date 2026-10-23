# -*- coding: utf-8 -*-
"""시장 스냅샷 생성 → data/market.json

출처는 Refinitiv(LSEG) Workspace(refinitiv_src). 이 계정에 권한이 없는 것만 예외로 남긴다.
- 국고·회사채 금리: 금융투자협회 최종호가수익률(15:30) — Refinitiv KOFIA OTC RIC(=KQ).
- 환율·지수·원자재: Refinitiv. VIX 는 권한이 없어 yfinance(사용자 결정 2026-10-23).
- 미국 금리: Refinitiv 벤치마크. ICE BofA IG·HY OAS 는 권한이 없어 FRED.
- 수동 입력: data/manual_inputs.json 이 있으면 "manual" 섹션으로 병합
  (스왑포인트, 헤지비용 등 자동화 어려운 항목용. 각 항목에 value/unit/as_of/source 필수).

모든 수치는 {value, unit, as_of, source} 묶음으로 저장한다.
"""
import datetime
import json
import math
import os

import config
import refinitiv_src as R

# (RIC, 키, 이름) — fetch_history.KOFIA_RICS 와 같은 RIC
KOFIA_RICS = [
    ("KRTSY2Y=KQ", "ktb_2y", "국고채 2년"),
    ("KRTSY3Y=KQ", "ktb_3y", "국고채 3년"),
    ("KRTSY5Y=KQ", "ktb_5y", "국고채 5년"),
    ("KRTSY10Y=KQ", "ktb_10y", "국고채 10년"),
    ("KRGUCORP=KQ", "corp_aa_minus_3y", "회사채 AA- 3년"),
]

# (yfinance 표기 티커, 키, 단위, 이름) — RIC 변환은 refinitiv_src.TICKER_RIC
MARKET_TICKERS = {
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
               ("^VIX", "vix", "pt", "VIX"),
               (".KSVKOSPI", "vkospi", "pt", "VKOSPI"),
               (".V2TX", "vstoxx", "pt", "VSTOXX")],
    "commodities": [("CL=F", "wti", "USD/bbl", "WTI 근월물"),
                    ("GC=F", "gold", "USD/oz", "금 현물")],
}

# (출처, id, 섹션, 키, 단위, bp 환산 여부, 이름) — fetch_history.US_SERIES 와 같은 시리즈
US_SNAPSHOT = [
    ("refinitiv", "US2YT=RR", "rates_us", "ust_2y", "%", False, "미국채 2년"),
    ("refinitiv", "US10YT=RR", "rates_us", "ust_10y", "%", False, "미국채 10년"),
    ("refinitiv", "USFFTARGET=", "rates_us", "fed_funds", "%", False, "연준 목표금리(범위 상단)"),
    ("refinitiv", "US10YT=RR - US10YTIP=RR", "rates_us", "breakeven_10y", "%", False, "미국 10년 BEI(명목−TIPS)"),
    ("fred", "BAMLC0A0CM", "credit_us", "ig_oas_bp", "bp", True, "ICE BofA 미국 IG OAS"),
    ("fred", "BAMLH0A0HYM2", "credit_us", "hy_oas_bp", "bp", True, "ICE BofA 미국 HY OAS"),
    ("fred", "BAMLC0A0CMEY", "credit_us", "ig_yield", "%", False, "ICE BofA 미국 IG 유효수익률"),
    ("fred", "BAMLH0A0HYM2EY", "credit_us", "hy_yield", "%", False, "ICE BofA 미국 HY 유효수익률"),
]


def _field(value, unit, as_of, source):
    return {"value": value, "unit": unit, "as_of": as_of, "source": source}


def _recent(expr: str):
    """최근 값 (값, 날짜). 'A - B' 는 두 RIC 의 차."""
    from fetch_history import _ref_series

    s = _ref_series(expr, datetime.date.today() - datetime.timedelta(days=21))
    if s.empty:
        raise ValueError("최근 21일 값 없음")
    return float(s.iloc[-1]), s.index[-1].date().isoformat()


def fetch_kr_rates() -> dict:
    """KOFIA 최종호가수익률 최근 영업일 값 + 커브·스프레드."""
    out = {}
    for ric, key, name in KOFIA_RICS:
        try:
            v, as_of = _recent(ric)
            out[key] = _field(round(v, 3), "%", as_of, f"Refinitiv {ric} (금융투자협회 최종호가수익률 15:30, {name})")
        except Exception as e:  # noqa: BLE001
            print(f"  경고: {name}({ric}) 수집 실패: {e}")
    if "ktb_3y" in out and "ktb_10y" in out:
        out["curve_3s10s_bp"] = _field(
            round((out["ktb_10y"]["value"] - out["ktb_3y"]["value"]) * 100, 1),
            "bp", out["ktb_10y"]["as_of"], "계산(ktb_10y - ktb_3y)")
    if "ktb_3y" in out and "corp_aa_minus_3y" in out:
        out["spread_aa_minus_bp"] = _field(
            round((out["corp_aa_minus_3y"]["value"] - out["ktb_3y"]["value"]) * 100, 1),
            "bp", out["ktb_3y"]["as_of"], "계산(corp_aa_minus_3y - ktb_3y)")
    return out


def fetch_markets() -> dict:
    out = {section: {} for section in MARKET_TICKERS}
    for section, items in MARKET_TICKERS.items():
        for ticker, key, unit, name in items:
            try:
                if ticker in R.YF_ONLY:
                    import yfinance as yf

                    h = yf.Ticker(ticker).history(period="5d")
                    if h.empty:
                        raise ValueError("빈 응답")
                    close, as_of = float(h.iloc[-1]["Close"]), h.index[-1].date().isoformat()
                else:
                    close, as_of = _recent(ticker)
                if not math.isfinite(close):
                    raise ValueError("종가가 NaN")
                out[section][key] = _field(
                    round(close, 4 if abs(close) < 10 else 2), unit, as_of,  # EUR/USD·USD/CNY 는 소수 4자리
                    f"{R.source_label(ticker)} 종가 ({name})")
            except Exception as e:  # noqa: BLE001 — 개별 티커 실패는 건너뜀
                print(f"  경고: {name}({ticker}) 수집 실패: {e}")
    return out


def fetch_us_rates() -> dict:
    """미국 금리(Refinitiv)·크레딧(FRED) 최신값. FRED 는 1영업일 늦게 반영된다(as_of 로 표시)."""
    from fetch_history import fetch_fred_series

    start = datetime.date.today() - datetime.timedelta(days=30)
    out = {"rates_us": {}, "credit_us": {}}
    for src, sid, section, key, unit, to_bp, name in US_SNAPSHOT:
        try:
            if src == "refinitiv":
                v, as_of = _recent(sid)
            else:
                s = fetch_fred_series(sid, start)
                if s.empty:
                    raise ValueError("최근 30일 값 없음")
                v, as_of = float(s.iloc[-1]), s.index[-1].date().isoformat()
            v *= 100 if to_bp else 1
            label = f"Refinitiv {sid}" if src == "refinitiv" else f"FRED {sid}"
            out[section][key] = _field(round(v, 1 if to_bp else 3), unit, as_of,
                                       f"{label} ({name})" + (" ×100 bp 환산" if to_bp else ""))
        except Exception as e:  # noqa: BLE001 — 개별 시리즈 실패는 건너뜀
            print(f"  경고: {name}({sid}) 수집 실패: {e}")
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

    if not R.available(force=True):
        # 미연결이면 기존 market.json 을 덮지 않는다 — 빈 스냅샷이 화면·에이전트로 가는 것보다 낫다
        raise RuntimeError(f"Refinitiv 미연결 — {R.status()['error']}")

    snapshot["rates_kr"] = fetch_kr_rates()
    print(f"  KOFIA 금리 {len([k for k in snapshot['rates_kr'] if not k.endswith('_bp')])}종 수집 완료")

    mk = fetch_markets()
    for section in MARKET_TICKERS:
        snapshot[section].update(mk.get(section, {}))

    us = fetch_us_rates()
    for section, fields in us.items():
        snapshot[section].update(fields)
    print(f"  미국 금리·크레딧 {sum(len(v) for v in us.values())}종 수집 완료")
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
