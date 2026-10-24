# -*- coding: utf-8 -*-
"""주식 대시보드 — 유니버스의 주식 항목을 공개 가격으로 채워 '지금 주식시장'을 한 화면에 보여 준다.

데이터: universe/equity.json 의 티커(Refinitiv — 표기는 yfinance 식 그대로, refinitiv_src 가 RIC 으로 바꾼다. 2005년~ 일별 종가, 현지 통화, 주식·ETF 는 배당 재투자 가격. VIX 만 yfinance) → data/history/equity_daily.csv(누적)
계산(CODE): 기간 수익률(1주·1개월·3개월·올해·1년), 1년 고점 대비, 60일 변동성, 흐름 점수(quant.tech_score),
           비교 기준 대비 앞선 정도(같은 날짜끼리 맞춘 가격비), 우선주/보통주 가격비 위치, 변동성 지수의 1년 중 위치.
출력: data/equity.json — 주식 탭·에이전트 컨텍스트가 쓴다. 쉬운 말 결론·타일도 여기서 만든다.

사용법: python equity.py
"""
import datetime
import json
import math
import os
import sys
import warnings

import pandas as pd

import config
import universe
from agent import load_json

HIST = config.DATA_DIR / "history" / "equity_daily.csv"
OUT = config.DATA_DIR / "equity.json"
START = "2005-01-01"
REGIONS = ["kospi", "sp500", "sx5e", "nikkei", "csi300", "nifty", "taiex"]   # 결론 문장에 쓰는 대표 지역


def refresh_prices(tickers, full: bool = False) -> pd.DataFrame:
    """처음엔 2005년부터, 이후엔 최근 3개월만 받아 잇는다(한 종목 실패는 기존 이력으로). 출처는 refinitiv_src."""
    warnings.filterwarnings("ignore")
    import refinitiv_src
    return refinitiv_src.refresh_frame(HIST, tickers, START, full=full)


def _clean(s: pd.Series) -> pd.Series:
    s = s.dropna()
    return s[s.index.dayofweek < 5]


# 기간은 달력 기준 — 빠진 날이 있어도 '7일 전·1개월 전' 같은 날짜끼리 비교한다(거래일 개수로 세면 시장마다 어긋남)
PERIODS = {"r1w": pd.Timedelta(days=7), "r1m": pd.DateOffset(months=1), "r3m": pd.DateOffset(months=3), "r1y": pd.DateOffset(years=1)}


def _ret(s: pd.Series, off) -> float | None:
    """마지막 값과 (마지막 날짜 − off) 이전의 가장 가까운 값의 수익률(%)."""
    prior = s[s.index <= s.index[-1] - off]
    return round(float(s.iloc[-1] / prior.iloc[-1] - 1) * 100, 2) if len(prior) else None


def metrics(s: pd.Series) -> dict | None:
    import quant
    s = _clean(s)
    if len(s) < 30:
        return None
    last, d = float(s.iloc[-1]), s.index[-1]
    before = s[s.index < pd.Timestamp(d.year, 1, 1)]
    r = s.pct_change().dropna()
    t = quant.tech_score(r)
    return {"last": round(last, 4), "as_of": d.date().isoformat(),
            "r1d": round(float(s.iloc[-1] / s.iloc[-2] - 1) * 100, 2), **{k: _ret(s, off) for k, off in PERIODS.items()},
            "ytd": round((last / float(before.iloc[-1]) - 1) * 100, 2) if len(before) else None,
            "dd52": round((last / float(s.tail(252).max()) - 1) * 100, 2),
            "vol60": round(float(r.tail(60).std() * math.sqrt(252) * 100), 1),
            "score": t["score"] if t else None, "flow": t["label"] if t else None, "tech": t}


def level_stats(s: pd.Series) -> dict | None:
    """변동성 지수처럼 '수준'이 의미 있는 시리즈 — 지금 값, 1년 중 위치, 1개월 변화."""
    s = _clean(s)
    if len(s) < 260:
        return None
    y = s.tail(252)
    last = float(s.iloc[-1])
    return {"last": round(last, 2), "as_of": s.index[-1].date().isoformat(), "pct_1y": round(float((y < last).mean()) * 100),
            "median_5y": round(float(s.tail(1260).median()), 1), "chg_1m": round(last - float(s.iloc[-22]), 2)}


def relative(s: pd.Series, b: pd.Series) -> dict:
    """같은 날짜끼리 맞춘 가격비로 '비교 기준보다 얼마나 앞섰나'(%p 에 가까운 %)."""
    df = pd.concat([_clean(s), _clean(b)], axis=1, join="inner").dropna()
    if len(df) < 70:
        return {}
    ratio = df.iloc[:, 0] / df.iloc[:, 1]
    return {"ex1m": _ret(ratio, PERIODS["r1m"]), "ex3m": _ret(ratio, PERIODS["r3m"]), "ex1y": _ret(ratio, PERIODS["r1y"])}


def _vix_word(v: float) -> str:
    return "낮음" if v < 15 else "보통" if v < 20 else "높음" if v < 30 else "매우 높음"


def _tone(x) -> str:
    return "" if x is None else ("good" if x > 0 else "bad" if x < 0 else "")


def build(save: bool = True, full: bool = False) -> dict:
    eq = universe.load_equity()
    reg = eq["series"]
    px = refresh_prices([r["ticker"] for r in reg], full=full)
    by_key = {r["key"]: r for r in reg}
    col = lambda k: px[by_key[k]["ticker"]] if k in by_key and by_key[k]["ticker"] in px else pd.Series(dtype=float)  # noqa: E731
    rows = {}
    for r in reg:
        s = col(r["key"])
        if r.get("level"):
            m = level_stats(s)
        else:
            m = metrics(s)
            if m and r.get("bench"):
                m.update(relative(s, col(r["bench"])))
                m["bench_name"] = by_key[r["bench"]]["short"]
        rows[r["key"]] = {"key": r["key"], "name": r["name"], "short": r["short"], "ticker": r["ticker"], "item": r.get("item"),
                          "group": r.get("group"), "note": r.get("note"), "ok": bool(m), **(m or {})}

    groups = [{"name": g, "rows": [rows[r["key"]] for r in reg if r.get("group") == g and rows[r["key"]]["ok"]]} for g in eq["groups"]]

    # 우선주/보통주 가격비 — 1년 범위 안 위치
    pref = None
    ps, cs = _clean(col("samsung_pref")), _clean(col("samsung"))
    if len(ps) > 260 and len(cs) > 260:
        df = pd.concat([ps, cs], axis=1, join="inner").dropna()
        ratio = df.iloc[:, 0] / df.iloc[:, 1]
        y = ratio.tail(252)
        last = float(ratio.iloc[-1])
        pos = (last - float(y.min())) / (float(y.max()) - float(y.min())) * 100 if float(y.max()) > float(y.min()) else 50
        pref = {"ratio": round(last, 3), "min_1y": round(float(y.min()), 3), "max_1y": round(float(y.max()), 3), "mean_1y": round(float(y.mean()), 3),
                "pos_1y": round(pos), "chg_1m": round((last / float(ratio.iloc[-22]) - 1) * 100, 2), "as_of": ratio.index[-1].date().isoformat(),
                "say": ("우선주가 보통주보다 1년 범위의 위쪽 — 상대적으로 비싼 편" if pos >= 70 else
                        "우선주가 보통주보다 1년 범위의 아래쪽 — 상대적으로 싼 편" if pos <= 30 else "우선주·보통주 가격비가 1년 범위의 가운데")}

    # 변동성 — 옵션 보험료(VIX)와 실제 흔들림
    vol = {k: rows.get(k) if rows.get(k, {}).get("ok") else None for k in ("vix", "vxn", "vkospi", "vstoxx")}
    for k in ("kospi200", "sp500"):
        s = _clean(col(k))
        if len(s) > 1300:
            rv = s.pct_change().rolling(60).std().dropna() * math.sqrt(252) * 100
            vol[f"{k}_rv"] = {"last": round(float(rv.iloc[-1]), 1), "pct_5y": round(float((rv.tail(1260) < rv.iloc[-1]).mean()) * 100)}
    if vol["vix"] and vol.get("sp500_rv"):
        gap = vol["vix"]["last"] - vol["sp500_rv"]["last"]
        vol["premium"] = {"gap": round(gap, 1), "say": (f"옵션 보험료(VIX)가 실제 흔들림보다 {gap:.1f}%p 높음 — 보험(풋 매수)은 비싼 편, 팔기(커버드콜)는 유리"
                                                         if gap >= 4 else f"옵션 보험료(VIX)가 실제 흔들림보다 {abs(gap):.1f}%p 낮음 — 보험(풋 매수)이 싼 편"
                                                         if gap <= 0 else f"옵션 보험료(VIX)가 실제 흔들림과 비슷({gap:+.1f}%p)")}

    # 쉬운 말 결론
    up = [rows[k]["short"] for k in REGIONS if rows[k].get("flow") in ("강세", "우호")]
    down = [rows[k]["short"] for k in REGIONS if rows[k].get("flow") in ("약세", "매우 약세")]
    parts = ([f"오르는 흐름: {'·'.join(up)}"] if up else []) + ([f"내리는 흐름: {'·'.join(down)}"] if down else [])
    vx = vol["vix"]["last"] if vol["vix"] else None
    if vx is not None:
        parts.append(f"변동성(VIX {vx:.1f}) {_vix_word(vx)}")
    lead = lambda g: sorted([r for r in rows.values() if r.get("group") == g and r.get("ex3m") is not None], key=lambda r: -r["ex3m"])  # noqa: E731
    st, se = lead("스타일·팩터"), lead("섹터·테마")
    sub = []
    if st:
        sub.append(f"앞서는 스타일: {st[0]['short']}({st[0]['ex3m']:+.1f}%p) · 뒤처지는 스타일: {st[-1]['short']}({st[-1]['ex3m']:+.1f}%p)")
    if se:
        sub.append(f"앞서는 업종: {se[0]['short']}({se[0]['ex3m']:+.1f}%p) · 뒤처지는 업종: {se[-1]['short']}({se[-1]['ex3m']:+.1f}%p)")
    tiles = []
    for k, lab in (("kospi", "한국 (KOSPI)"), ("sp500", "미국 (S&P500)")):
        r = rows[k]
        if r["ok"]:
            tiles.append({"label": f"{lab} 1개월", "value": f"{r['r1m']:+.1f}%", "tone": _tone(r["r1m"]),
                          "sub": f"흐름 {r['flow'] or '-'} · 1년 고점 대비 {r['dd52']:.1f}% · {r['as_of'][5:]}"})
    if vx is not None:
        tiles.append({"label": "변동성 (VIX)", "value": f"{vx:.1f} · {_vix_word(vx)}", "tone": "bad" if vx >= 25 else "",
                      "sub": f"지난 1년 중 아래에서 {vol['vix']['pct_1y']}% 위치 · 5년 중앙값 {vol['vix']['median_5y']}"})
    if st and se:
        tiles.append({"label": "앞서는 스타일 · 업종 (3개월, 시장 대비)", "value": f"{st[0]['short']} · {se[0]['short']}", "tone": "good",
                      "sub": f"{st[0]['ex3m']:+.1f}%p · {se[0]['ex3m']:+.1f}%p"})

    v = universe.equity_view()
    have = {k for k, r in rows.items() if r["ok"]}
    for s in v["strategies"]:
        s["data_ok"] = [k for k in s["data"] if k in have]
    cnt = {k: sum(1 for s in v["strategies"] if s["ready"] == k) for k in ("ready", "proxy", "need")}
    dates = sorted({r["as_of"] for r in rows.values() if r.get("ok") and r.get("as_of")})
    d = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
         "price_dates": [dates[0], dates[-1]] if dates else [],
         "basis": "Refinitiv(VIX 만 yfinance) · 현지 통화 기준(환율 제외) · 주식·ETF 는 배당 재투자 반영 가격",
         "verdict": " · ".join(parts), "sub": " / ".join(sub), "tiles": tiles,
         "groups": groups, "pref": pref, "pff": rows.get("pff"), "vol": vol,
         "universe": v, "readiness_count": cnt, "missing": eq.get("missing", [])}
    if save:
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, OUT)
    return d


KR_RATES = [("bok_base", "기준금리"), ("cd_91d", "CD91"), ("ktb3y_base", "국고3년−기준"),
            ("spread_bbb", "BBB- 스프레드"), ("ks200_basis", "베이시스")]


def kr_rates() -> list[dict] | None:
    """주식 탭 '시장 한눈에' 위 한 줄 — 국내 금리·크레딧·선물 베이시스의 수준과 1주·1개월 변화(analytics 시계열).
    변화 단위: 금리·스프레드는 bp, 베이시스는 %p. 시계열을 못 읽으면 None(화면은 줄을 숨긴다)."""
    try:
        import analytics
        df = analytics.load_panel()
        out = []
        for col, label in KR_RATES:
            s = analytics.stats(col, df)
            if not s or s.get("value") is None:
                continue
            pp = col == "ks200_basis"   # stats 는 rate 변화를 ×100(bp) 으로 준다 — 베이시스는 %p 로 되돌린다
            cv = lambda x: None if x is None else (round(x / 100, 2) if pp else x)  # noqa: E731
            out.append({"key": col, "label": label, "name": s["name"], "value": s["value"], "unit": s["unit"],
                        "as_of": s["as_of"], "chg_1w": cv(s.get("chg_1w")), "chg_1m": cv(s.get("chg_1m")),
                        "chg_unit": "%p" if pp else "bp", "source": s["source"]})
        return out or None
    except Exception:  # noqa: BLE001 — 보조 줄이라 주식 탭 전체를 막지 않는다
        return None


_SPARK: dict = {}


def sparks() -> dict:
    """홈 점수판 차트용 1년 일별 값 — 주식 유니버스(equity_daily.csv)와 정량 스캐너(analytics 패널, 금리는 가격 환산).
    항목마다 {d0: 첫 날짜, t: d0 부터 날짜 수, v: 값, m: 200일 이동평균}. 원본 파일이 바뀔 때만 다시 계산한다."""
    import analytics
    import quant
    files = [HIST] + [analytics.HIST_DIR / f for f in analytics.HIST_FILES] + [config.DATA_DIR / "market.json"]
    key = tuple((p.name, p.stat().st_mtime) for p in files if p.exists())
    if _SPARK.get("key") == key:
        return _SPARK["d"]
    g = lambda x: None if x is None or not math.isfinite(x) else float(f"{x:.6g}")  # noqa: E731

    def pack(s: pd.Series) -> dict | None:
        s = _clean(s)
        if len(s) < 30:
            return None
        ma = s.rolling(200).mean()
        w = s[s.index > s.index[-1] - pd.DateOffset(years=1)]
        d0 = w.index[0]
        return {"d0": d0.date().isoformat(), "t": [(d - d0).days for d in w.index], "v": [g(float(x)) for x in w],
                "m": [g(float(x)) for x in ma.reindex(w.index)]}

    out = {"eq": {}, "q": {}}
    if HIST.exists():
        px = pd.read_csv(HIST, index_col=0, parse_dates=True)
        for r in universe.load_equity()["series"]:
            if not r.get("level") and r["ticker"] in px:
                p = pack(px[r["ticker"]])
                if p:
                    out["eq"][r["key"]] = p
    panel = analytics.load_panel()
    for col, _name, mult in quant.SCAN:
        if col not in panel:
            continue
        s = _clean(panel[col])
        nav = (1 + mult * s.diff().fillna(0) / 100).cumprod() * 100 if mult < 0 else s   # 금리는 scan() 과 같은 가격 환산 지수
        p = pack(nav)
        if p:
            out["q"][col] = p
    _SPARK.update(key=key, d=out)
    return out


def load(max_age_hours: float | None = None) -> dict | None:
    d = load_json(OUT)
    if d and max_age_hours is not None:
        age = datetime.datetime.now() - datetime.datetime.fromisoformat(d["generated_at"])
        if age.total_seconds() > max_age_hours * 3600:
            return None
    if d is not None:
        d["kr_rates"] = kr_rates()
    return d


def age_hours() -> float | None:
    d = load_json(OUT)
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(d["generated_at"])).total_seconds() / 3600
    except (TypeError, KeyError, ValueError):
        return None


def to_markdown(d: dict) -> str:
    """에이전트 컨텍스트용 요약(공개 데이터라 외부 전송 가능)."""
    f = lambda x: "-" if x is None else f"{x:+.1f}%"  # noqa: E731
    L = [f"## 주식 시장 대시보드 (CODE · {d['basis']} · 가격 {' ~ '.join(d['price_dates'])})", d["verdict"], d["sub"], "",
         "| 그룹 | 이름 | 1개월 | 3개월 | 올해 | 1년 고점 대비 | 흐름 | 3개월 시장 대비 |", "|---|---|---|---|---|---|---|---|"]
    for g in d["groups"]:
        if g["name"] == "변동성":
            continue
        for r in g["rows"]:
            ex = "-" if r.get("ex3m") is None else f"{r['ex3m']:+.1f}%p ({r.get('bench_name')} 대비)"
            L.append(f"| {g['name']} | {r['name']} | {f(r.get('r1m'))} | {f(r.get('r3m'))} | {f(r.get('ytd'))} | {f(r.get('dd52'))} | "
                     f"{r.get('flow') or '-'} | {ex} |")
    vol = d.get("vol") or {}
    if vol.get("vix"):
        L.append(f"\n변동성: VIX {vol['vix']['last']} (1년 중 {vol['vix']['pct_1y']}% 위치)"
                 + "".join(f", {n} {vol[k]['last']} (1년 중 {vol[k]['pct_1y']}% 위치)"
                           for k, n in (("vkospi", "VKOSPI"), ("vstoxx", "VSTOXX")) if vol.get(k))
                 + (f", KOSPI200 60일 실제 변동성 {vol['kospi200_rv']['last']}%" if vol.get("kospi200_rv") else "")
                 + (f" — {vol['premium']['say']}" if vol.get("premium") else ""))
    if d.get("pref"):
        L.append(f"우선주: 삼성전자우/보통 가격비 {d['pref']['ratio']} — {d['pref']['say']}")
    c = d.get("readiness_count", {})
    L.append(f"주식 결합 전략 {len(d['universe']['strategies'])}개(유니버스 문서) — 백테스트 준비: 바로 가능 {c.get('ready', 0)} · "
             f"근사 {c.get('proxy', 0)} · 데이터 필요 {c.get('need', 0)}.")
    import backtest
    bt = backtest.load_summary()
    if bt:
        L += ["", f"### 주식 전략 백테스트 (CODE · 기본 설정 · {bt['period']} · {bt['generated_at'][:10]} · 과거 데이터, 매매 지시 아님)",
              "| 전략 | 연 수익률 | 변동성 | 최대 낙폭 | 샤프 | 그냥 보유 |", "|---|---|---|---|---|---|"]
        for x in bt["rows"]:
            if not x.get("error"):
                held = f"{x['bench_name']} 연 {x['bench_cagr']}% · 낙폭 {x['bench_mdd']}%" if x.get("bench_name") else "현금 위 초과수익"
                L.append(f"| {x['name']} | {x['cagr']}% | {x['vol']}% | {x['mdd']}% | {x['sharpe']} | {held} |")
    else:
        L.append("기대수익·리스크는 아직 백테스트 전(숫자를 지어내지 마라).")
    return "\n".join(L)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(to_markdown(build(full="--full" in sys.argv)))   # --full: 이력 전체를 다시 받는다
