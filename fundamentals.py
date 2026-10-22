# -*- coding: utf-8 -*-
"""주식 유니버스 밸류에이션 — 항목별 PER·EPS·PBR·배당과 상위 10종목(종목마다 같은 숫자).

출처(공개 데이터):
  해외 = yfinance — ETF 의 PER·PBR·배당·보수·순자산·상위 보유종목·업종 비중, 종목의 PER·선행 PER·EPS·선행 EPS·PBR·배당·ROE.
         지수 자체는 이익 데이터가 없어 대표 ETF 로 대신한다(S&P500 = SPY, 나스닥100 = QQQ …).
  국내 = 네이버 증권 모바일 API — 종목 PER·EPS·추정 PER·추정 EPS·PBR·BPS·배당, ETF 상위 10종목·업종·배당·보수,
         지수 시가총액 상위 10종목. 해외 ETF 안의 한국 종목도 네이버 숫자로 맞춘다.
계산(CODE): 상위 10종목 묶음 PER·선행 PER·PBR(비중 가중 조화평균 = 묶음 시가총액 ÷ 묶음 이익),
           이익 증가 예상(선행 EPS ÷ EPS − 1, 묶음은 PER ÷ 선행 PER − 1), 지수 환산 EPS(지수 ÷ PER),
           시장 대비 비쌈·쌈(국내 = KOSPI200 상위 10종목 묶음, 그 밖 = S&P500 의 PER 과 비교, ±15% 안은 '비슷').
출력: data/equity_fund.json — 주식 탭 '유니버스'와 STOCK 에이전트 컨텍스트가 쓴다. 12시간 지나면 웹이 백그라운드로 다시 받는다.

사용법: python fundamentals.py
"""
import datetime
import json
import math
import os
import re
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor

import requests

import config
import universe

OUT = config.DATA_DIR / "equity_fund.json"
NAVER = "https://m.stock.naver.com/api"
UA = {"User-Agent": "Mozilla/5.0"}
STALE_HOURS = 12

# 항목 → 어디서 받나: (종류, 코드, 메모). 종류 yf = 해외 ETF(또는 지수를 대신하는 ETF), nv_etf = 국내 ETF,
# nv_index = 국내 지수(시총 상위 10종목), nv_stock = 국내 종목. 여기 없는 주식 항목은 자기 티커로 yf.
SOURCE = {
    "kospi": ("nv_index", "KOSPI", "종목은 시가총액 상위 10 · 시장 PER 은 MSCI 한국(EWY)"),
    "kospi200": ("nv_etf", "069500", "KODEX 200 구성 기준"),
    "kosdaq": ("nv_etf", "229200", "KODEX 코스닥150 구성 기준"),
    "sp500": ("yf", "SPY", "SPY(S&P500 ETF) 기준"),
    "ndx": ("yf", "QQQ", "QQQ(나스닥100 ETF) 기준"),
    "sx5e": ("yf", "FEZ", "FEZ(유로스톡스50 ETF) 기준"),
    "nikkei": ("yf", "EWJ", "닛케이 대신 MSCI 일본(EWJ) 기준"),
    "csi300": ("yf", "ASHR", "ASHR(CSI300 ETF) 기준"),
    "hsi": ("yf", "EWH", "항셍 대신 MSCI 홍콩(EWH) 기준"),
    "nifty": ("yf", "INDA", "Nifty 대신 MSCI 인도(INDA) 기준"),
    "taiex": ("yf", "EWT", "가권 대신 MSCI 대만(EWT) 기준"),
    "kr_div": ("nv_etf", "161510", ""),
    "s_bank_kr": ("nv_etf", "091170", ""),
    "s_semi_kr": ("nv_etf", "091160", ""),
    "s_hc_kr": ("nv_etf", "266420", ""),
    "samsung": ("nv_stock", "005930", ""),
    "samsung_pref": ("nv_stock", "005935", ""),
}
INDEX_EPS = {"sp500", "ndx", "sx5e"}          # ETF 가 그 지수를 그대로 따라가 '지수 ÷ PER' 이 뜻이 있는 것
NO_PER = {"pff": "우선주 ETF 라 PER 대신 배당을 봅니다"}
KR_MARKET_PER = "EWY"                          # KOSPI 항목의 시장 PER(MSCI 한국)
SECTOR_KO = {
    "IT": "IT", "technology": "IT", "HEALTHCARE": "헬스케어", "healthcare": "헬스케어",
    "INDUSTRIALS": "산업재", "industrials": "산업재", "COMMUNICATION": "커뮤니케이션", "communication_services": "커뮤니케이션",
    "MATERIALS": "소재", "basic_materials": "소재", "CONSUMER_DISCRETIONARY": "경기소비재", "consumer_cyclical": "경기소비재",
    "CONSUMER_STAPLES": "필수소비재", "consumer_defensive": "필수소비재", "FINANCIALS": "금융", "financial_services": "금융",
    "ENERGY": "에너지", "energy": "에너지", "REAL_ESTATE": "부동산", "realestate": "부동산", "real_estate": "부동산",
    "UTILITIES": "유틸리티", "utilities": "유틸리티", "UNCLASSIFIED": "기타",
}


# ── 숫자 정리 ──
def _f(v):
    """float 로(빈칸·NaN·문자 'N/A' 는 None)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _nv_num(s):
    """네이버 '9.62배' · '22,292원' · '0.78%' · '-1,234원' → 숫자."""
    if s is None:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(s))
    return _f(m.group(0).replace(",", "")) if m else None


def _nv_won(s):
    """네이버 '1,254조 268억' · '8,977억' → 원."""
    if not s:
        return None
    jo = re.search(r"([\d,]+)조", s)
    eok = re.search(r"([\d,]+)억", s)
    if not jo and not eok:
        return None
    v = (int(jo.group(1).replace(",", "")) * 10000 if jo else 0) + (int(eok.group(1).replace(",", "")) if eok else 0)
    return v * 1e8


def _pos(x):
    return x if x is not None and x > 0 else None


def _growth(eps, feps):
    """선행 EPS ÷ EPS − 1 (%) — 적자에서 흑자 전환처럼 기준이 음수·0이면 None."""
    return round((feps / eps - 1) * 100, 1) if eps and feps and eps > 0 else None


def _r(x, d=2):
    return round(x, d) if x is not None else None


# ── 받아오기 ──
def _nv_get(path):
    r = requests.get(f"{NAVER}/{path}", headers=UA, timeout=12)
    r.raise_for_status()
    return r.json()


def nv_stock(code: str) -> dict:
    """국내 종목 — PER·EPS 는 최근 실적(네이버 기준 시점), 추정치는 컨센서스."""
    d = _nv_get(f"stock/{code}/integration")
    t = {x["code"]: x for x in d.get("totalInfos") or []}
    v = lambda k: _nv_num((t.get(k) or {}).get("value"))  # noqa: E731
    eps, feps, bps = v("eps"), v("cnsEps"), v("bps")
    return {"code": code, "name": d.get("stockName") or code, "src": "네이버", "ccy": "KRW",
            "price": v("lastClosePrice"), "per": _pos(v("per")), "fper": _pos(v("cnsPer")), "eps": eps, "feps": feps,
            "pbr": _pos(v("pbr")), "bps": bps, "div": v("dividendYieldRatio"), "dps": v("dividend"),
            "roe": _r(eps / bps * 100, 1) if eps is not None and bps else None,
            "mcap": _nv_won((t.get("marketValue") or {}).get("value")), "growth": _growth(eps, feps),
            "eps_asof": (t.get("eps") or {}).get("valueDesc"), "foreign": (t.get("foreignRate") or {}).get("value")}


def yf_info(ticker: str) -> dict:
    import yfinance as yf
    return yf.Ticker(ticker).info or {}


def yf_stock(ticker: str) -> dict:
    """해외 종목 — 한국 종목 코드(6자리)는 네이버로 돌린다(해외 ETF 안의 삼성전자 등).
    ETF 보유종목에 접미사 없이 오는 숫자 코드: 4~5자리 = 홍콩, 5로 시작하는 6자리 = 인도 BSE, 그 밖 6자리 = 한국."""
    sym = ticker
    if re.fullmatch(r"\d{4,5}", ticker):
        sym = f"{int(ticker):04d}.HK"
    elif re.fullmatch(r"5\d{5}", ticker):
        sym = f"{ticker}.BO"
    else:
        m = re.match(r"^(\d{6})(\.(KS|KQ))?$", ticker)
        if m:
            return {**nv_stock(m.group(1)), "code": ticker}
    i = yf_info(sym)
    eps, feps = _f(i.get("trailingEps")), _f(i.get("forwardEps"))
    return {"code": ticker, "name": i.get("shortName") or i.get("longName") or ticker, "src": "yfinance", "ccy": i.get("currency"),
            "price": _f(i.get("regularMarketPrice") or i.get("currentPrice")), "per": _pos(_f(i.get("trailingPE"))),
            "fper": _pos(_f(i.get("forwardPE"))), "eps": eps, "feps": feps, "pbr": _pos(_f(i.get("priceToBook"))),
            "div": _f(i.get("dividendYield")), "roe": _r((_f(i.get("returnOnEquity")) or 0) * 100, 1) if i.get("returnOnEquity") is not None else None,
            "mcap": _f(i.get("marketCap")), "growth": _growth(eps, feps), "sector": SECTOR_KO.get(str(i.get("sector", "")).lower().replace(" ", "_"), i.get("sector"))}


def yf_etf(ticker: str) -> dict:
    """해외 ETF — 자기 PER·PBR·배당·보수 + 상위 10 보유종목(비중) + 업종 비중."""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    i = tk.info or {}
    out = {"ticker": ticker, "ccy": i.get("currency"), "per": _pos(_f(i.get("trailingPE"))), "pbr": _pos(_f(i.get("priceToBook"))),
           "div": _r((_f(i.get("yield")) or 0) * 100) if i.get("yield") is not None else _f(i.get("dividendYield")),
           "fee": _f(i.get("netExpenseRatio")), "aum": _f(i.get("totalAssets")), "issuer": i.get("fundFamily"),
           "category": i.get("category"), "price": _f(i.get("regularMarketPrice")), "holdings": [], "sectors": []}
    try:
        fd = tk.funds_data
        th = fd.top_holdings
        out["holdings"] = [{"code": str(sym), "name": str(row.get("Name") or sym), "w": _r(_f(row.get("Holding Percent")) * 100 if _f(row.get("Holding Percent")) is not None else None)}
                           for sym, row in th.head(10).iterrows()]
        out["sectors"] = sorted(({"name": SECTOR_KO.get(k, k), "w": _r(_f(v) * 100, 1)} for k, v in (fd.sector_weightings or {}).items() if _f(v)),
                                key=lambda x: -x["w"])
    except Exception as e:  # noqa: BLE001 — 보유종목이 없는 ETF 도 있다
        out["holdings_error"] = str(e)
    return out


def nv_etf(code: str) -> dict:
    a = _nv_get(f"stock/{code}/etfAnalysis")
    ki = (_nv_get(f"stock/{code}/integration").get("etfKeyIndicator") or {})
    return {"ticker": code, "ccy": "KRW", "name": a.get("itemName"), "div": _f(ki.get("dividendYieldTtm")), "fee": _f(a.get("totalFee")),
            "aum": _nv_won(a.get("totalNav")), "issuer": a.get("issuerName"), "base_index": a.get("etfBaseIndex"),
            "holdings": [{"code": x["itemCode"], "name": x.get("itemName"), "w": _nv_num(x.get("etfWeight"))}
                         for x in a.get("etfTop10MajorConstituentAssets") or [] if re.fullmatch(r"\d{6}", str(x.get("itemCode") or ""))],
            "sectors": sorted(({"name": SECTOR_KO.get(x["detailTypeCode"], x["detailTypeCode"]), "w": _f(x.get("weight"))}
                               for x in a.get("sectorPortfolioList") or [] if _f(x.get("weight"))), key=lambda x: -x["w"])}


def nv_index(code: str) -> dict:
    d = _nv_get(f"index/{code}/integration")
    return {"ticker": code, "ccy": "KRW", "holdings": [{"code": x["itemCode"], "name": x.get("stockName"), "w": None,
                                                         "mcap_raw": _f(x.get("marketValueRaw"))} for x in d.get("enrollStocks") or []],
            "sectors": []}


# ── 묶음 계산 ──
def basket(hs: list) -> dict:
    """상위 10종목 묶음: PER = Σ비중 ÷ Σ(비중 ÷ PER) — 비중만큼 산 묶음의 시가총액 ÷ 이익. 적자(PER 없음)는 빼고 그만큼 '덮은 비중'을 적는다."""
    def harm(k):
        ws = [(h["_w"], h[k]) for h in hs if h.get("_w") and h.get(k)]
        tw = sum(w for w, _ in ws)
        return (_r(tw / sum(w / x for w, x in ws), 1), tw) if ws else (None, 0)
    total = sum(h["_w"] for h in hs if h.get("_w")) or 0
    per, cov = harm("per")
    fper, _ = harm("fper")
    pbr, _ = harm("pbr")
    dws = [(h["_w"], h["div"]) for h in hs if h.get("_w") and h.get("div") is not None]
    return {"per": per, "fper": fper, "pbr": pbr,
            "div": _r(sum(w * x for w, x in dws) / sum(w for w, _ in dws), 2) if dws else None,
            "growth": _r((per / fper - 1) * 100, 1) if per and fper else None,
            "cover": _r(cov / total * 100, 0) if total else None}


def _fetch_all(fn, keys, workers=8):
    keys = list(dict.fromkeys(keys))
    out = {}

    def one(k):
        try:
            return k, fn(k)
        except Exception as e:  # noqa: BLE001 — 한 종목 실패가 전체를 막지 않게
            return k, {"code": k, "error": f"{type(e).__name__}: {e}"}
    with ThreadPoolExecutor(workers) as ex:
        for k, v in ex.map(one, keys):
            out[k] = v
    return out


def _vs(per, ref_per, ref_name):
    if not per or not ref_per:
        return None
    diff = round((per / ref_per - 1) * 100)
    word = "비슷" if abs(diff) < 15 else ("쌈" if diff < 0 else "비쌈")
    return {"ref_name": ref_name, "ref_per": ref_per, "diff": diff, "word": word,
            "text": f"{ref_name}과 비슷" if word == "비슷" else f"{ref_name}보다 {abs(diff)}% {word}"}


def _say(x):
    """쉬운 말 한 줄 — 결론(비쌈·쌈) → 이익 전망."""
    parts = []
    per = x.get("per") or (x.get("basket") or {}).get("per")
    if x.get("no_per"):
        parts.append(f"{x['no_per']} — 배당 {x['div']:.1f}%" if x.get("div") is not None else x["no_per"])
    elif per:
        parts.append(f"이익의 {per:.1f}배 가격(PER)" + (f" — {x['vs']['text']}" if x.get("vs") else ""))
    g = x.get("growth")
    if g is not None:
        who = "이 종목" if x["kind"] == "nv_stock" else "상위 10종목"
        gi = math.floor(abs(g) + 0.5) * (1 if g >= 0 else -1)   # 화면(JS)과 같은 사사오입
        parts.append(f"{who} 이익이 앞으로 1년 {'+' if gi >= 0 else ''}{gi}% {'늘' if gi >= 0 else '줄'} 것으로 예상")
    return " · ".join(parts) or "이익 데이터가 없습니다"


def build(save: bool = True) -> dict:
    warnings.filterwarnings("ignore")
    eq = universe.load_equity()
    last = {}
    try:   # 지수 환산 EPS 에 쓰는 지수 값(주식 대시보드의 마지막 가격)
        import equity
        for g in (equity.load() or {}).get("groups", []):
            for r in g.get("rows", []):
                last[r["key"]] = r.get("last")
    except Exception:  # noqa: BLE001
        pass
    series = [s for s in eq["series"] if s.get("item") and s.get("group") not in (None, "변동성")]
    plan = {s["key"]: SOURCE.get(s["key"], ("yf", s["ticker"], "")) for s in series}

    # 1) 항목(ETF·지수·종목) 자체
    fns = {"yf": yf_etf, "nv_etf": nv_etf, "nv_index": nv_index, "nv_stock": nv_stock}
    heads = {}
    for kind, fn in fns.items():
        heads.update({(kind, c): v for c, v in _fetch_all(fn, [c for k, c, _ in plan.values() if k == kind]).items()})
    ewy = _fetch_all(yf_etf, [KR_MARKET_PER]).get(KR_MARKET_PER, {})

    # 2) 상위 10종목 — 국내 코드는 네이버, 해외는 yfinance(안의 한국 종목은 네이버)
    nv_codes, yf_codes = [], []
    for (kind, _), h in heads.items():
        for x in h.get("holdings") or []:
            (nv_codes if kind.startswith("nv_") else yf_codes).append(x["code"])
    stocks = {**_fetch_all(nv_stock, nv_codes, 6), **_fetch_all(yf_stock, yf_codes, 8)}

    items = []
    for s in series:
        kind, code, memo = plan[s["key"]]
        h = heads.get((kind, code), {})
        x = {"key": s["key"], "name": s["name"], "group": s["group"], "item_id": s["item"], "kind": kind, "code": code,
             "src": "네이버 증권" if kind.startswith("nv_") else "yfinance", "memo": memo, "error": h.get("error")}
        if kind == "nv_stock":
            x.update({k: h.get(k) for k in ("price", "per", "fper", "eps", "feps", "pbr", "bps", "div", "dps", "roe", "mcap", "growth", "eps_asof", "foreign", "ccy")})
        else:
            hs = []
            for hd in h.get("holdings") or []:
                st = stocks.get(hd["code"], {})
                w = hd.get("w") if hd.get("w") is not None else None
                hs.append({**st, "code": hd["code"], "name": st.get("name") if st.get("src") == "네이버" else (hd.get("name") or st.get("name")),
                           "w": w, "_w": w if w is not None else (hd.get("mcap_raw") or st.get("mcap"))})
            b = basket(hs)
            x.update({"ccy": h.get("ccy"), "fee": h.get("fee"), "aum": h.get("aum"), "issuer": h.get("issuer"), "category": h.get("category"),
                      "base_index": h.get("base_index"), "sectors": (h.get("sectors") or [])[:8], "basket": b,
                      "holdings": [{k: v for k, v in y.items() if k != "_w"} for y in hs], "holdings_by": "weight" if kind != "nv_index" else "mcap",
                      "top_w": _r(sum(y["w"] for y in hs if y.get("w")), 1) if kind != "nv_index" else None})
            if kind == "yf":
                x.update({"per": h.get("per"), "pbr": h.get("pbr"), "div": h.get("div"), "fper": b["fper"],
                          "growth": b["growth"]})
            elif kind == "nv_index":   # KOSPI: 시장 전체 PER 은 MSCI 한국 ETF
                x.update({"per": ewy.get("per"), "pbr": ewy.get("pbr"), "div": ewy.get("div"), "fper": b["fper"], "growth": b["growth"],
                          "per_src": "MSCI 한국(EWY)"})
            else:                      # 국내 ETF: PER 은 상위 10종목 묶음
                x.update({"per": b["per"], "pbr": b["pbr"], "div": h.get("div"), "fper": b["fper"], "growth": b["growth"],
                          "per_src": "상위 10종목 묶음"})
            if s["key"] in INDEX_EPS and x.get("per") and last.get(s["key"]):
                x["index_eps"] = _r(last[s["key"]] / x["per"], 1)
                x["index_last"] = last[s["key"]]
        if s["key"] in NO_PER:
            x["no_per"] = NO_PER[s["key"]]
            x["per"] = x["fper"] = x["growth"] = None
        items.append(x)

    # 3) 시장 대비 — 국내는 KOSPI200 상위 10종목 묶음, 나머지는 S&P500
    by = {x["key"]: x for x in items}
    ref_kr, ref_us = (by.get("kospi200") or {}).get("per"), (by.get("sp500") or {}).get("per")
    for x in items:
        kr = x["kind"].startswith("nv_") and x["key"] not in ("kospi", "kospi200", "kosdaq")
        if x["key"] in ("sp500", "kospi200") or x.get("no_per"):
            continue
        x["vs"] = _vs(x.get("per"), ref_kr, "KOSPI200") if kr else _vs(x.get("per"), ref_us, "S&P500")
    for x in items:
        x["say"] = _say(x)

    items_doc = {it["id"]: it for it in universe.load()["plain_vanilla"] if it["asset"] == "주식"}
    d = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
         "groups": [{"id": k, "name": v["item"], "examples": v["examples"]} for k, v in items_doc.items()],
         "items": items,
         "notes": ["PER·PBR 은 최근 실적 기준, 선행 PER·추정 EPS 는 애널리스트 1년 예상(컨센서스)입니다.",
                   "지수는 이익 데이터가 없어 대표 ETF 로 대신했습니다(항목마다 '기준' 표시).",
                   "국내 ETF 의 PER 은 상위 10종목 묶음 기준(비중 가중) — 나머지 종목은 빠져 있습니다.",
                   "출처: yfinance(해외), 네이버 증권(국내) · 공개 데이터 · 매매 지시 아님."]}
    if save:
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, OUT)
    return d


def load() -> dict | None:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def age_hours() -> float | None:
    d = load()
    if not d:
        return None
    return (datetime.datetime.now() - datetime.datetime.fromisoformat(d["generated_at"])).total_seconds() / 3600


def to_markdown(d: dict) -> str:
    """STOCK 에이전트 컨텍스트 — 항목별 한 줄(공개 데이터)."""
    n = lambda v, s="": "-" if v is None else f"{v:.1f}{s}"  # noqa: E731
    lines = [f"## 주식 유니버스 밸류에이션 ({d['generated_at'][:16]} · 공개 데이터)",
             "| 항목 | PER | 선행 PER | 이익 증가 예상 | PBR | 배당 | 시장 대비 | 기준 |", "|---|---|---|---|---|---|---|---|"]
    for x in d["items"]:
        lines.append(f"| {x['name']} | {n(x.get('per'), '배')} | {n(x.get('fper'), '배')} | {n(x.get('growth'), '%')} | {n(x.get('pbr'), '배')} | "
                     f"{n(x.get('div'), '%')} | {(x.get('vs') or {}).get('text', '-')} | {x.get('memo') or x.get('per_src') or x['src']} |")
    lines += [f"- {t}" for t in d.get("notes", [])]
    return "\n".join(lines)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = datetime.datetime.now()
    d = build()
    for x in d["items"]:
        print(f"{x['name']:<24} {x['say']}" + (f"  [오류 {x['error']}]" if x.get("error") else ""))
    print(f"{(datetime.datetime.now() - t0).total_seconds():.0f}초 · {OUT}")
