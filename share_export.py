# -*- coding: utf-8 -*-
"""공유용 스냅샷 — 주식 시장 흐름 + 유니버스 밸류에이션을 claude.ai 공유 대시보드에 올릴 JSON 으로 뽑는다.

공개 데이터(yfinance·네이버 증권)에서 나온 숫자만 담는다. 넣지 않는 것: 내부 유니버스 문서의 전략 목록·문서 ID·메모,
인포맥스 데이터(config.SHARE_VENDOR_DATA), 포트·한도, 회사 이름.
입력: data/equity.json, data/equity_fund.json → 출력: data/share/*.json (market_summary · market_tiles · regions · valuation · holdings · sectors)

사용법: python share_export.py  (그다음 대시보드에 파일을 다시 올려 달라고 Claude 에게 요청)
"""
import json
import sys

import config
import equity
import fundamentals

OUT = config.DATA_DIR / "share"


def _r(v, d):
    return round(v, d) if isinstance(v, (int, float)) else None


def _eps_d(ccy):
    """원·엔은 정수, 그 밖은 소수 2자리(화면 표기와 같게)."""
    return 0 if ccy in ("KRW", "JPY") else 2


def build() -> dict:
    eq, fd = equity.load(), fundamentals.load()
    if not eq or not fd:
        raise SystemExit("data/equity.json 또는 data/equity_fund.json 이 없습니다 — 주식 탭에서 먼저 받아 주세요.")
    dates = eq.get("price_dates") or []
    summary = [{"verdict": eq["verdict"], "sub": eq.get("sub", ""), "computed_at": eq["generated_at"][:16].replace("T", " "),
                "price_from": dates[0] if dates else None, "price_to": dates[-1] if dates else None,
                "valuation_at": fd["generated_at"][:16].replace("T", " ")}]
    tiles = [{"order": i + 1, "label": t["label"], "value": t["value"], "note": t.get("sub", ""), "tone": t.get("tone") or ""}
             for i, t in enumerate(eq.get("tiles") or [])]
    regions = []
    for g in eq["groups"]:
        if g["name"] not in ("국내", "선진국", "신흥국"):
            continue
        for r in g["rows"]:
            last = r.get("last")
            regions.append({"key": r["key"], "region": g["name"], "name": r["name"],
                            "last": _r(last, 0 if last is not None and abs(last) >= 1000 else 2),   # 화면 자릿수와 같게
                            "r1w": _r(r.get("r1w"), 1), "r1m": _r(r.get("r1m"), 1), "r3m": _r(r.get("r3m"), 1),
                            "ytd": _r(r.get("ytd"), 1), "from_high": _r(r.get("dd52"), 1), "trend": r.get("flow") or "",
                            "as_of": r.get("as_of")})
    group_name = {g["id"]: g["name"] for g in fd["groups"]}
    valuation, holdings, sectors = [], [], []
    for x in fd["items"]:
        vs = x.get("vs") or {}
        valuation.append({"key": x["key"], "group": group_name.get(x["item_id"], ""), "name": x["name"],
                          "basis": x.get("memo") or x.get("per_src") or "", "per": _r(x.get("per"), 1), "fper": _r(x.get("fper"), 1),
                          "growth": _r(x.get("growth"), 0), "pbr": _r(x.get("pbr"), 2), "div": _r(x.get("div"), 2),
                          "vs_market": ("비슷" if vs.get("word") == "비슷" else f"{abs(vs['diff'])}% {vs['word']}") if vs
                          else ("배당으로 봄" if x.get("no_per") else ""), "vs_tone": vs.get("word") or "", "vs_diff": vs.get("diff"),
                          "vs_ref": vs.get("ref_name") or "", "summary": x["say"], "source": x["src"],
                          "kind": "종목" if x["kind"] == "nv_stock" else "지수·ETF",
                          "eps": _r(x.get("eps"), _eps_d(x.get("ccy"))), "feps": _r(x.get("feps"), _eps_d(x.get("ccy"))), "ccy": x.get("ccy") or "",
                          "index_eps": _r(x.get("index_eps"), 0), "fee": _r(x.get("fee"), 2)})
        for i, h in enumerate(x.get("holdings") or [], 1):
            holdings.append({"item": x["key"], "rank": i, "code": h["code"], "name": h.get("name") or h["code"],
                             "weight": _r(h.get("w"), 1), "mcap_trn": _r(h["mcap"] / 1e12, 1) if h.get("mcap") else None,
                             "per": _r(h.get("per"), 1), "fper": _r(h.get("fper"), 1), "eps": _r(h.get("eps"), _eps_d(h.get("ccy"))),
                             "feps": _r(h.get("feps"), _eps_d(h.get("ccy"))), "growth": _r(h.get("growth"), 0), "pbr": _r(h.get("pbr"), 2),
                             "div": _r(h.get("div"), 2), "ccy": h.get("ccy") or ""})
        for s in x.get("sectors") or []:
            if (s.get("w") or 0) >= 0.5:
                sectors.append({"item": x["key"], "sector": s["name"], "weight": _r(s["w"], 1)})
    return {"market_summary": summary, "market_tiles": tiles, "regions": regions,
            "valuation": valuation, "holdings": holdings, "sectors": sectors}


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rows in build().items():
        (OUT / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{name}: {len(rows)}행")
    print(OUT)
