# -*- coding: utf-8 -*-
"""주식 유니버스 밸류에이션 — 항목별 PER·EPS·PBR·배당과 상위 10종목(종목마다 같은 숫자).

출처: Refinitiv(LSEG) Workspace(refinitiv_src).
  ETF = 구성 종목 전체와 비중(TR.FundHoldingRIC·TR.FundPercentageOfFundAssets), 배당수익률·총보수·순자산·운용사.
  종목 = PER(TR.PE, 최근 실적), 선행 PER(TR.PtoEPSMeanEst, 컨센서스), 추정 EPS(TR.EPSMean), PBR, 배당수익률,
         시가총액, GICS 업종, ROE(TR.F.ReturnAvgTotEqPct).
  지수 자체는 이익 데이터가 없어 대표 ETF 로 대신한다(S&P500 = SPY, 나스닥100 = QQQ, KOSPI = MSCI 한국 EWY …).
  국내 ETF 의 NAV·괴리율은 이 단말에서 안 나온다(README §4) — 쓰지 않는다.
계산(CODE): 묶음 PER·선행 PER·PBR(비중 가중 조화평균 = 묶음 시가총액 ÷ 묶음 이익). ETF 는 구성 종목 '전체'로 계산하고
           화면에는 상위 10종목만 보인다. 이익 증가 예상(선행 EPS ÷ EPS − 1, 묶음은 PER ÷ 선행 PER − 1),
           지수 환산 EPS(지수 ÷ PER), 업종 비중(구성 종목 GICS 합산),
           시장 대비 비쌈·쌈(국내 = KOSPI200 묶음, 그 밖 = S&P500 의 PER 과 비교, ±15% 안은 '비슷').
출력: data/equity_fund.json — 주식 탭 '유니버스'와 STOCK 에이전트 컨텍스트가 쓴다. 12시간 지나면 웹이 백그라운드로 다시 받는다.

사용법: python fundamentals.py
"""
import datetime
import json
import math
import os
import sys
import warnings

import config
import refinitiv_src as R
import universe

OUT = config.DATA_DIR / "equity_fund.json"
STALE_HOURS = 12

# 항목 → 어디서 받나: (종류, yfinance 표기 코드, 메모). 종류 etf = ETF(또는 지수를 대신하는 ETF), stock = 종목.
# 여기 없는 주식 항목은 자기 티커를 ETF 로 본다.
SOURCE = {
    "kospi": ("etf", "EWY", "KOSPI 대신 MSCI 한국(EWY) 구성 기준"),
    "kospi200": ("etf", "069500.KS", "KODEX 200 구성 기준"),
    "kosdaq": ("etf", "229200.KS", "KODEX 코스닥150 구성 기준"),
    "sp500": ("etf", "SPY", "SPY(S&P500 ETF) 기준"),
    "ndx": ("etf", "QQQ", "QQQ(나스닥100 ETF) 기준"),
    "sx5e": ("etf", "FEZ", "FEZ(유로스톡스50 ETF) 기준"),
    "nikkei": ("etf", "EWJ", "닛케이 대신 MSCI 일본(EWJ) 기준"),
    "csi300": ("etf", "ASHR", "ASHR(CSI300 ETF) 기준"),
    "hsi": ("etf", "EWH", "항셍 대신 MSCI 홍콩(EWH) 기준"),
    "nifty": ("etf", "INDA", "Nifty 대신 MSCI 인도(INDA) 기준"),
    "taiex": ("etf", "EWT", "가권 대신 MSCI 대만(EWT) 기준"),
    "kr_div": ("etf", "161510.KS", ""),
    "s_bank_kr": ("etf", "091170.KS", ""),
    "s_semi_kr": ("etf", "091160.KS", ""),
    "s_hc_kr": ("etf", "266420.KS", ""),
    "samsung": ("stock", "005930.KS", ""),
    "samsung_pref": ("stock", "005935.KS", ""),
}
INDEX_EPS = {"sp500", "ndx", "sx5e"}          # ETF 가 그 지수를 그대로 따라가 '지수 ÷ PER' 이 뜻이 있는 것
NO_PER = {"pff": "우선주 ETF 라 PER 대신 배당을 봅니다"}
SECTOR_KO = {
    "Information Technology": "IT", "Health Care": "헬스케어", "Industrials": "산업재",
    "Communication Services": "커뮤니케이션", "Materials": "소재", "Consumer Discretionary": "경기소비재",
    "Consumer Staples": "필수소비재", "Financials": "금융", "Energy": "에너지", "Real Estate": "부동산",
    "Utilities": "유틸리티",
}
STOCK_FIELDS = ["TR.CommonName", "TR.PriceClose", "TR.PE", "TR.PtoEPSMeanEst", "TR.EPSMean",
                "TR.PriceToBVPerShare", "TR.DividendYield", "TR.CompanyMarketCap", "TR.GICSSector",
                "TR.F.ReturnAvgTotEqPct"]
ETF_FIELDS = ["TR.CommonName", "TR.PriceClose", "TR.DividendYield", "TR.FundTER", "TR.FundTotalNetAssets",
              "TR.FundCompany"]


# ── 숫자 정리 ──
def _f(v):
    """float 로(빈칸·NaN·문자 'N/A' 는 None)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _pos(x):
    return x if x is not None and x > 0 else None


def _growth(eps, feps):
    """선행 EPS ÷ EPS − 1 (%) — 적자에서 흑자 전환처럼 기준이 음수·0이면 None."""
    return round((feps / eps - 1) * 100, 1) if eps and feps and eps > 0 else None


def _r(x, d=2):
    return round(x, d) if x is not None else None


# ── 받아오기 ──
def _ccy(ric: str) -> str:
    return {".KS": "KRW", ".KQ": "KRW", ".T": "JPY", ".HK": "HKD", ".SS": "CNY", ".SZ": "CNY", ".TW": "TWD",
            ".NS": "INR", ".BO": "INR"}.get("." + ric.rsplit(".", 1)[-1], "USD") if "." in ric else "USD"


def _rows(rics: list[str], fields: list[str], batch: int = 500) -> dict[str, list]:
    """get_data 를 batch 개씩 — {RIC: [필드값…]} (요청한 필드 순서)."""
    out = {}
    for i in range(0, len(rics), batch):
        df, _ = R.snapshot(rics[i:i + batch], fields)
        for row in df.itertuples(index=False):
            out[str(row[0])] = [None if (v is None or (isinstance(v, float) and v != v) or str(v) in ("", "<NA>")) else v
                                for v in row[1:]]
    return out


def stocks(rics: list[str]) -> dict[str, dict]:
    """종목 지표 — PER 은 최근 실적, 선행 PER·추정 EPS 는 컨센서스(1년 예상). EPS = 가격 ÷ PER."""
    rics = list(dict.fromkeys(r for r in rics if r))
    out = {}
    for ric, v in _rows(rics, STOCK_FIELDS).items():
        name, price, per, fper, feps, pbr, div, mcap, sector, roe = v
        per, fper, pbr, price = _pos(_f(per)), _pos(_f(fper)), _pos(_f(pbr)), _f(price)
        eps = _r(price / per, 2) if price and per else None
        out[ric] = {"code": ric, "name": name or ric, "src": "Refinitiv", "ccy": _ccy(ric), "price": price,
                    "per": _r(per), "fper": _r(fper), "eps": eps, "feps": _r(_f(feps)), "pbr": _r(pbr),
                    "bps": _r(price / pbr, 2) if price and pbr else None, "div": _r(_f(div)),
                    "roe": _r(_f(roe), 1), "mcap": _f(mcap), "growth": _growth(eps, _f(feps)),
                    "sector": SECTOR_KO.get(sector, sector) if sector else None}
    return out


def etfs(codes: list[str]) -> dict[str, dict]:
    """ETF — 자기 배당·보수·순자산 + 구성 종목 전체(비중 %)."""
    rics = {c: R.ric_for(c) for c in dict.fromkeys(codes)}
    heads = _rows(list(rics.values()), ETF_FIELDS)
    out = {}
    for code, ric in rics.items():
        name, price, div, fee, aum, issuer = heads.get(ric) or [None] * len(ETF_FIELDS)
        x = {"ticker": code, "ric": ric, "ccy": _ccy(ric), "name": name, "price": _f(price), "div": _r(_f(div)),
             "fee": _f(fee), "aum": _f(aum), "issuer": issuer, "holdings": []}
        try:
            df, _ = R.snapshot([ric], ["TR.FundHoldingRIC", "TR.FundPercentageOfFundAssets"], {"EndNum": 5000})
            hs = [{"code": str(r[1]), "w": _f(r[2])} for r in df.itertuples(index=False)
                  if r[1] and str(r[1]) not in ("<NA>", "nan") and _f(r[2])]
            x["holdings"] = sorted(hs, key=lambda h: -h["w"])
        except Exception as e:  # noqa: BLE001 — 구성 종목이 없는 ETF 도 있다
            x["holdings_error"] = str(e)
        out[code] = x
    return out


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
        who = "이 종목" if x["kind"] == "stock" else "구성 종목"
        gi = math.floor(abs(g) + 0.5) * (1 if g >= 0 else -1)   # 화면(JS)과 같은 사사오입
        parts.append(f"{who} 이익이 앞으로 1년 {'+' if gi >= 0 else ''}{gi}% {'늘' if gi >= 0 else '줄'} 것으로 예상")
    return " · ".join(parts) or "이익 데이터가 없습니다"


def _sectors(hs: list) -> list:
    acc = {}
    for h in hs:
        if h.get("_w") and h.get("sector"):
            acc[h["sector"]] = acc.get(h["sector"], 0) + h["_w"]
    tot = sum(acc.values())
    return sorted(({"name": k, "w": _r(v / tot * 100, 1)} for k, v in acc.items()), key=lambda x: -x["w"]) if tot else []


def build(save: bool = True) -> dict:
    warnings.filterwarnings("ignore")
    if not R.available(force=True):
        raise RuntimeError(f"Refinitiv 미연결 — {R.status()['error']}")
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
    plan = {s["key"]: SOURCE.get(s["key"], ("etf", s["ticker"], "")) for s in series}

    # 1) ETF 머리 정보 + 구성 종목 전체, 2) 구성 종목과 단일 종목 항목의 지표를 한 번에
    heads = etfs([c for k, c, _ in plan.values() if k == "etf"])
    single = {c: R.ric_for(c) for k, c, _ in plan.values() if k == "stock"}
    st = stocks([h["code"] for x in heads.values() for h in x["holdings"]] + list(single.values()))

    items = []
    for s in series:
        kind, code, memo = plan[s["key"]]
        x = {"key": s["key"], "name": s["name"], "group": s["group"], "item_id": s["item"], "kind": kind, "code": code,
             "src": "Refinitiv", "memo": memo}
        if kind == "stock":
            h = st.get(single[code], {})
            x["error"] = None if h else "Refinitiv 값 없음"
            x.update({k: h.get(k) for k in ("price", "per", "fper", "eps", "feps", "pbr", "bps", "div", "roe", "mcap", "growth", "ccy")})
        else:
            h = heads.get(code, {})
            x["error"] = h.get("holdings_error") or (None if h.get("holdings") else "구성 종목 없음")
            hs = [{**st.get(hd["code"], {"code": hd["code"], "name": hd["code"]}), "code": hd["code"], "w": _r(hd["w"]), "_w": hd["w"]}
                  for hd in h.get("holdings") or []]
            b = basket(hs)
            top = hs[:10]
            x.update({"ccy": h.get("ccy"), "fee": h.get("fee"), "aum": h.get("aum"), "issuer": h.get("issuer"),
                      "sectors": _sectors(hs)[:8], "basket": b, "n_holdings": len(hs),
                      "holdings": [{k: v for k, v in y.items() if k != "_w"} for y in top], "holdings_by": "weight",
                      "top_w": _r(sum(y["w"] for y in top if y.get("w")), 1),
                      "per": b["per"], "pbr": b["pbr"], "div": h.get("div"), "fper": b["fper"], "growth": b["growth"],
                      "per_src": f"구성 {len(hs)}종목 묶음"})
            if s["key"] in INDEX_EPS and x.get("per") and last.get(s["key"]):
                x["index_eps"] = _r(last[s["key"]] / x["per"], 1)
                x["index_last"] = last[s["key"]]
        if s["key"] in NO_PER:
            x["no_per"] = NO_PER[s["key"]]
            x["per"] = x["fper"] = x["growth"] = None
        items.append(x)

    # 3) 시장 대비 — 국내는 KOSPI200 묶음, 나머지는 S&P500
    by = {x["key"]: x for x in items}
    ref_kr, ref_us = (by.get("kospi200") or {}).get("per"), (by.get("sp500") or {}).get("per")
    for x in items:
        kr = str(x["code"]).endswith((".KS", ".KQ")) and x["key"] not in ("kospi", "kospi200", "kosdaq")
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
                   "ETF 의 PER 은 구성 종목 전체의 비중 가중 묶음 기준(적자 종목은 제외, '덮은 비중' 표시).",
                   "출처: Refinitiv(LSEG) · 로컬 전용(재배포 금지) · 매매 지시 아님."]}
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
    """STOCK 에이전트 컨텍스트 — 항목별 한 줄."""
    n = lambda v, s="": "-" if v is None else f"{v:.1f}{s}"  # noqa: E731
    lines = [f"## 주식 유니버스 밸류에이션 ({d['generated_at'][:16]} · Refinitiv)",
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
