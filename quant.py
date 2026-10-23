# -*- coding: utf-8 -*-
"""정량 분석 — 기술적 매력도 + 과거 변동성·국면을 고려한 예상손익 (전부 코드 계산, 에이전트는 해석만).

포지션 = 다리(leg)들의 일별 수익률을 현재 시가 비중으로 합친 '포지션 수익률' 시계열(롱 +, 숏 −).
- 기술적 매력도(−100~+100): 포지션 NAV 의 추세(MA200 대비)·6개월 모멘텀·1개월 모멘텀(변동성 대비)·단기 과열(RSI14 역방향).
  관례적 휴리스틱이며 매매 신호가 아니다.
- 예상손익(1·6개월):
  ① 변동성 기준 — EWMA(λ=0.94) 일변동성, 추세 없음, 4,000개 경로 몬테카를로.
  ② 국면 기준 — 포지션 상태(3·6개월 수익률·1개월 변동성·1년 고점 대비)와 시장 위험선호(VIX)가 지금과 비슷한
     과거 날짜를 robust z 거리로 골라(인접 21영업일 중복 제거), 그 날 이후 실제 경로를 그대로 재현.
  둘 다 손익 분위수(5·25·50·75·95%)와 손절·목표 중 먼저 닿을 확률을 낸다.
- 포트 전체: 같은 날짜의 경로를 함께 써 상관을 반영 → 기간 중 손실이 손실한도를 넘을 확률.
- 기회 스캐너: 시장 지표(지수·환율·원자재·금리 가격 환산)의 기술적 매력도와 국면 기준 6개월 중앙값.

가격: data/history/legs_daily.csv (Refinitiv 전체 이력, 증분 갱신). 대용 — KOSPI200 선물=KODEX 200, 10년 국채선물=국고10y×듀레이션,
달러·엔선물=현물환. 헤지된 해외 자산은 현지 가격 수익률(환 제외).

사용법:  python quant.py [--json]      결과는 data/quant.json
"""
import datetime
import json
import math
import os
import sys
import warnings

import numpy as np
import pandas as pd

import config
from agent import load_json

LEG_HIST = config.DATA_DIR / "history" / "legs_daily.csv"
OUT = config.DATA_DIR / "quant.json"
HORIZONS = {"1M": 21, "6M": 126}
N_PATHS = 4000
K_ANALOG = 25
GAP_DAYS = 21


# ── 가격 이력 ────────────────────────────────────────────────────
def leg_source(leg: dict) -> tuple[str, str]:
    n, m, code = leg.get("name", ""), leg.get("market", ""), str(leg.get("code", ""))
    if "KOSPI200 선물" in n:
        return "yf", "069500.KS"          # 지수 이력 대용: KODEX 200
    if "국채선물" in n:
        return "ktb", "ktb_10y"
    if "달러선물" in n:
        return "yf", "KRW=X"
    if "엔선물" in n:
        return "yf", "JPYKRW=X"
    if m.startswith("KRX"):
        return "yf", f"{code}.KS"
    return "yf", code


def refresh_prices(symbols: set[str], full: bool = False) -> pd.DataFrame:
    """처음엔 전체 이력(1990~), 이후엔 최근 3개월만 받아 잇는다. 출처는 refinitiv_src."""
    warnings.filterwarnings("ignore")
    import refinitiv_src
    return refinitiv_src.refresh_frame(LEG_HIST, symbols, "1990-01-01", full=full)


def _ktb_returns(dur: float) -> pd.Series:
    import analytics
    y = analytics.load_panel()["ktb_10y"].dropna()
    return (-dur * y.diff() / 100).rename("ktb")


def position_returns(pos: dict, px: pd.DataFrame) -> pd.Series | None:
    """포지션 일별 수익률 = Σ 부호 × (다리 시가 / 기준) × 다리 수익률. 기준 = 롱 시가 합(환 오버레이는 명목)."""
    legs = pos.get("legs") or []
    longs = [l for l in legs if l.get("side") != "숏"]
    base = sum(float(l.get("mv_bn") or 0) for l in longs) or sum(float(l.get("mv_bn") or 0) for l in legs)
    if not base:
        return None
    parts = []
    for l in legs:
        kind, sym = leg_source(l)
        if kind == "ktb":
            r = _ktb_returns(float(l.get("dur") or 7.9))
        else:
            if sym not in px:
                return None
            r = px[sym].dropna().pct_change(fill_method=None)
        w = (-1 if l.get("side") == "숏" else 1) * float(l.get("mv_bn") or 0) / base
        parts.append((w * r).rename(sym + l.get("side", "")))
    df = pd.concat(parts, axis=1, sort=True)
    start = max(s.first_valid_index() for _, s in df.items())
    df = df[df.index >= start].fillna(0.0)          # 휴장일 불일치는 0 수익률
    r = df.sum(axis=1)
    return r[r.index.dayofweek < 5]


# ── 기술적 매력도 ────────────────────────────────────────────────
def _rsi(nav: pd.Series, n: int = 14) -> float:
    d = nav.diff().dropna().tail(250)
    up = d.clip(lower=0).ewm(alpha=1 / n).mean().iloc[-1]
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n).mean().iloc[-1]
    return float(100 - 100 / (1 + up / dn)) if dn > 0 else 100.0


def tech_score(r: pd.Series) -> dict | None:
    r = r.dropna()
    if len(r) < 260:
        return None
    nav = (1 + r).cumprod()
    sig = float(r.tail(252).std() * math.sqrt(252)) or 1e-9
    ma200 = float(nav.tail(200).mean())
    ret = lambda n: float(nav.iloc[-1] / nav.iloc[-n - 1] - 1)  # noqa: E731
    trend = math.tanh((nav.iloc[-1] / ma200 - 1) / (sig * math.sqrt(200 / 252)))
    mom6 = math.tanh(ret(126) / (sig * math.sqrt(0.5)))
    mom1 = math.tanh(ret(21) / (sig * math.sqrt(1 / 12)))
    rsi = _rsi(nav)
    rev = -math.tanh((rsi - 50) / 25)
    score = 100 * (0.35 * trend + 0.35 * mom6 + 0.15 * mom1 + 0.15 * rev)
    label = "강세" if score >= 30 else "우호" if score >= 10 else "중립" if score > -10 else "약세" if score > -30 else "매우 약세"
    hi = float(nav.tail(252).max())
    return {"score": round(score, 1), "label": label, "trend": round(trend, 2), "mom6": round(mom6, 2), "mom1": round(mom1, 2),
            "rsi": round(rsi, 1), "ret_1m": round(ret(21) * 100, 2), "ret_3m": round(ret(63) * 100, 2),
            "ret_6m": round(ret(126) * 100, 2), "vol_ann": round(sig * 100, 1), "dd_1y": round((nav.iloc[-1] / hi - 1) * 100, 2)}


# ── 예상손익 ────────────────────────────────────────────────────
def _barrier_stats(paths: np.ndarray, stop: float | None, target: float | None) -> dict:
    """paths: (n, h) 누적수익률. 손절·목표 중 먼저 닿은 비율과 기말 분위수."""
    n, h = paths.shape
    big = h + 1
    t_stop = np.where((paths <= stop).any(1), (paths <= stop).argmax(1), big) if stop is not None else np.full(n, big)
    t_tgt = np.where((paths >= target).any(1), (paths >= target).argmax(1), big) if target is not None else np.full(n, big)
    term = paths[:, -1]
    q = np.percentile(term, [5, 25, 50, 75, 95])
    return {"q_pct": [round(float(x) * 100, 2) for x in q], "mean_pct": round(float(term.mean()) * 100, 2),
            "p_stop": round(float((t_stop < t_tgt).mean()) * 100, 1), "p_target": round(float((t_tgt < t_stop).mean()) * 100, 1),
            "worst_path_pct": round(float(np.percentile(paths.min(1), 5)) * 100, 2), "n": int(n)}


def vol_paths(r: pd.Series, h: int, seed: int = 7) -> tuple[np.ndarray, float]:
    sig = float(np.sqrt((r.tail(500) ** 2).ewm(alpha=0.06).mean().iloc[-1]))   # EWMA λ=0.94
    rng = np.random.default_rng(seed)
    steps = rng.normal(-0.5 * sig * sig, sig, size=(N_PATHS, h))
    return np.expm1(np.cumsum(steps, axis=1)), sig


def _features(r: pd.Series, vix: pd.Series | None) -> pd.DataFrame:
    nav = (1 + r).cumprod()
    f = pd.DataFrame({"ret3m": nav.pct_change(63), "ret6m": nav.pct_change(126),
                      "vol1m": r.rolling(21).std() * math.sqrt(252), "dd1y": nav / nav.rolling(252).max() - 1})
    if vix is not None:
        f["vix"] = vix.reindex(f.index).ffill(limit=5)
    return f.dropna()


def analog_paths(r: pd.Series, h: int, vix: pd.Series | None) -> tuple[np.ndarray, list, list]:
    """지금과 비슷한 과거 날짜(이후 h일 경로가 있는 날만)에서 실제 누적 경로를 꺼낸다."""
    f = _features(r, vix)
    if len(f) < 300:
        return np.empty((0, h)), [], ["SHORT_HISTORY"]
    cur = f.iloc[-1]
    med = f.median(); mad = ((f - med).abs().median() * 1.4826).replace(0, np.nan)
    z = ((f - med) / mad).clip(-3, 3); zc = ((cur - med) / mad).clip(-3, 3)
    pos = {d: i for i, d in enumerate(r.index)}
    last_ok = len(r) - h - 1
    cand = z[[pos.get(d, 10**9) <= last_ok for d in z.index]]
    dist = np.sqrt(((cand - zc) ** 2).sum(axis=1)).sort_values()
    picked = []
    for d in dist.index:
        if all(abs((d - p).days) >= GAP_DAYS * 7 // 5 for p in picked):
            picked.append(d)
        if len(picked) >= K_ANALOG:
            break
    rv = r.values
    paths = np.array([np.cumprod(1 + rv[pos[d] + 1: pos[d] + 1 + h]) - 1 for d in picked]) if picked else np.empty((0, h))
    flags = []
    if len(picked) < 12:
        flags.append("LOW_SAMPLE")
    if (r.index[-1] - r.index[0]).days < 365 * 6:
        flags.append("SHORT_HISTORY")
    years = sorted({d.year for d in picked})
    if picked and len(years) <= 2:
        flags.append("SINGLE_REGIME")
    return paths, [d.date().isoformat() for d in sorted(picked)], flags


def analyze_position(pos: dict, px: pd.DataFrame, vix: pd.Series | None) -> dict:
    r = position_returns(pos, px)
    is_fx = pos.get("asset") == "fx"
    base = float(pos.get("fx_exposure_bn") if is_fx else (pos.get("mv_bn") or 0)) or 0.0
    stop = (pos.get("fx_stop_pct") if is_fx else pos.get("stop_loss_pct"))
    tgt = pos.get("target_pct")
    out = {"id": pos["id"], "name": pos.get("short_name") or pos["name"], "group": pos.get("group"),
           "strategy": pos.get("strategy"), "base_bn": round(base, 1),
           "stop_pct": stop, "target_pct": tgt, "tech": None, "vol": {}, "analog": {}}
    if r is None or len(r.dropna()) < 260:
        out["note"] = "가격 이력 부족"
        return out
    out["history_from"] = r.index[0].date().isoformat()
    out["tech"] = tech_score(r)
    s, t = (stop / 100 if stop is not None else None), (tgt / 100 if tgt is not None else None)
    for hk, h in HORIZONS.items():
        vp, sig = vol_paths(r, h)
        v = _barrier_stats(vp, s, t); v["sigma_ann_pct"] = round(sig * math.sqrt(252) * 100, 1)
        v["q_bn"] = [round(base * x / 100, 1) for x in v["q_pct"]]
        out["vol"][hk] = v
        ap, dates, flags = analog_paths(r, h, vix)
        if len(ap):
            a = _barrier_stats(ap, s, t); a["q_bn"] = [round(base * x / 100, 1) for x in a["q_pct"]]
            a["flags"] = flags; a["dates"] = dates if hk == "6M" else []
            out["analog"][hk] = a
        else:
            out["analog"][hk] = {"n": 0, "flags": flags}
    return out


def _market_features(panel: pd.DataFrame) -> pd.DataFrame:
    p = panel.ffill(limit=5)
    return pd.DataFrame({"kospi3m": p["kospi"].pct_change(63, fill_method=None), "vix": p["vix"],
                         "ktb3m": p["ktb_3y"].diff(63), "krw3m": p["usdkrw"].pct_change(63, fill_method=None)}).dropna()


def analyze_portfolio(results: list[dict], series: dict[str, pd.Series], limit_bn: float, panel: pd.DataFrame) -> dict:
    """포지션별 '기준 × 수익률'(억)을 같은 날짜로 합친 포트 손익 — 같은 날의 경로를 써서 상관 반영."""
    cols = {res["id"]: series[res["id"]] * res["base_bn"] for res in results if res["id"] in series and res["base_bn"]}
    df = pd.DataFrame(cols).dropna(how="all")
    start = max(s.first_valid_index() for s in cols.values())
    df = df[df.index >= start].fillna(0.0)
    pnl = df.sum(axis=1)
    h = HORIZONS["6M"]
    out = {"limit_bn": limit_bn, "history_from": pnl.index[0].date().isoformat(), "n_positions": len(cols)}
    # ① 변동성 기준 — 포트 일손익의 EWMA 표준편차, 정규 경로(억 단위 합산)
    sig = float(np.sqrt((pnl.tail(500) ** 2).ewm(alpha=0.06).mean().iloc[-1]))
    rng = np.random.default_rng(11)
    paths = np.cumsum(rng.normal(0, sig, size=(N_PATHS, h)), axis=1)
    out["vol"] = {"sigma_daily_bn": round(sig, 1), "q_bn": [round(float(x), 1) for x in np.percentile(paths[:, -1], [5, 25, 50, 75, 95])],
                  "p_breach": round(float((paths.min(1) <= -limit_bn).mean()) * 100, 1), "n": N_PATHS}
    # 전체 노출을 k 배로 줄이면 경로도 k 배 → 초과 확률을 p% 로 낮추는 k = 한도 / |기간 중 최저 손익의 p 분위|
    mins = paths.min(1)
    out["vol"]["scale_for_breach"] = {str(p): round(min(1.0, limit_bn / abs(float(np.percentile(mins, p)))), 3)
                                      for p in (10, 20, 30)}
    # ② 국면 기준 — 시장 국면(KOSPI 3개월·VIX·국고3y 3개월 변화·원달러 3개월)이 비슷한 날의 이후 6개월 실제 포트 경로
    mf = _market_features(panel)
    mf = mf[mf.index.isin(pnl.index)]
    idx = {d: i for i, d in enumerate(pnl.index)}
    cand = mf[[idx[d] <= len(pnl) - h - 1 for d in mf.index]]
    if len(cand) > 50:
        cur = _market_features(panel).iloc[-1]
        med = cand.median(); mad = ((cand - med).abs().median() * 1.4826).replace(0, np.nan)
        dist = np.sqrt(((((cand - med) / mad).clip(-3, 3) - ((cur - med) / mad).clip(-3, 3)) ** 2).sum(axis=1)).sort_values()
        picked = []
        for d in dist.index:
            if all(abs((d - p).days) >= 30 for p in picked):
                picked.append(d)
            if len(picked) >= K_ANALOG:
                break
        pv = pnl.values
        ap = np.array([np.cumsum(pv[idx[d] + 1: idx[d] + 1 + h]) for d in picked])
        out["analog"] = {"q_bn": [round(float(x), 1) for x in np.percentile(ap[:, -1], [5, 25, 50, 75, 95])],
                         "p_breach": round(float((ap.min(1) <= -limit_bn).mean()) * 100, 1), "n": len(picked),
                         "dates": [d.date().isoformat() for d in sorted(picked)],
                         "flags": (["LOW_SAMPLE"] if len(picked) < 12 else []) + (["SHORT_HISTORY"] if (pnl.index[-1] - pnl.index[0]).days < 365 * 6 else [])}
    else:
        out["analog"] = {"n": 0, "flags": ["SHORT_HISTORY"]}
    # 위험 기여: 변동성 기준 6개월 손익 분산에 대한 포지션별 기여(공분산 비중)
    cov = df.tail(500).cov()
    w = np.ones(len(df.columns))
    tot = float(w @ cov.values @ w)
    contrib = (cov.values @ w) / tot if tot > 0 else np.zeros(len(w))
    out["risk_contrib"] = sorted([{"id": c, "pct": round(float(x) * 100, 1)} for c, x in zip(df.columns, contrib)], key=lambda x: -x["pct"])
    return out


# ── 기회 스캐너 ──────────────────────────────────────────────────
SCAN = [("kospi", "KOSPI", 1), ("kosdaq", "KOSDAQ", 1), ("sp500", "S&P500", 1), ("nasdaq", "나스닥", 1), ("nikkei", "닛케이225", 1),
        ("stoxx50", "유로스톡스50", 1), ("eem", "신흥국(EEM)", 1), ("usdkrw", "USD/KRW", 1), ("usdjpy", "USD/JPY", 1),
        ("eurusd", "EUR/USD", 1), ("dxy", "달러인덱스", 1), ("gold", "금", 1), ("wti", "WTI", 1),
        ("ktb_10y", "국고10년 (가격 환산)", -8.0), ("ust_10y_cmt", "미국채10년 (가격 환산)", -8.0)]


def scan(panel: pd.DataFrame) -> list[dict]:
    vix = panel["vix"].dropna() if "vix" in panel else None
    rows = []
    for col, name, mult in SCAN:
        if col not in panel:
            continue
        s = panel[col].dropna()
        s = s[s.index.dayofweek < 5]
        r = (mult * s.diff() / 100) if mult < 0 else s.pct_change(fill_method=None)   # 금리는 듀레이션 8 가격 수익률로
        r = r.dropna()
        t = tech_score(r)
        if not t:
            continue
        ap, _, flags = analog_paths(r, HORIZONS["6M"], vix)
        med = round(float(np.median(ap[:, -1])) * 100, 2) if len(ap) else None
        up = round(float((ap[:, -1] > 0).mean()) * 100) if len(ap) else None
        rows.append({"key": col, "name": name, **t, "analog_6m_median_pct": med, "analog_6m_up_pct": up, "flags": flags,
                     "as_of": s.index[-1].date().isoformat()})
    return sorted(rows, key=lambda x: -x["score"])


# ── 실행 ────────────────────────────────────────────────────────
def run(save: bool = True, full: bool = False) -> dict:
    import analytics
    import limits
    doc = limits.load_positions()
    positions = doc.get("positions", [])
    syms = {leg_source(l)[1] for p in positions for l in (p.get("legs") or []) if leg_source(l)[0] == "yf"}
    px = refresh_prices(syms, full=full)
    panel = analytics.load_panel()
    vix = panel["vix"].dropna() if "vix" in panel else None
    results, series = [], {}
    for p in positions:
        res = analyze_position(p, px, vix)
        results.append(res)
        r = position_returns(p, px)
        if r is not None:
            series[p["id"]] = r
    lim = limits.load_limits()
    out = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "positions_as_of": doc.get("meta", {}).get("as_of"), "is_trial": "가상 시험 포트" in (doc.get("meta", {}).get("note") or ""),
           "price_last": px.index[-1].date().isoformat() if len(px) else None,
           "horizons": HORIZONS, "positions": results,
           "portfolio": analyze_portfolio(results, series, float(lim.get("total_limit_bn") or 0), panel),
           "scanner": scan(panel),
           "method": {"tech": "추세(MA200 대비)·6개월/1개월 모멘텀(변동성 대비)·RSI14 역방향 가중합, −100~+100. 관례적 휴리스틱.",
                      "vol": f"EWMA(λ=0.94) 일변동성·추세 없음·{N_PATHS:,}개 경로", "analog": f"포지션 상태+VIX 가 비슷한 과거 {K_ANALOG}개 날짜의 실제 이후 경로(21영업일 중복 제거)",
                      "portfolio": "포지션 손익(억)을 같은 날짜로 합산 — 상관 반영. 국면 기준은 KOSPI 3개월·VIX·국고3y 3개월 변화·원달러 3개월",
                      "proxy": "KOSPI200 선물=KODEX 200, 10년 국채선물=국고10y×듀레이션, 달러·엔선물=현물환. 헤지 자산은 환 제외 현지가격."}}
    if save:
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, OUT)
        snap = config.DATA_DIR / "quant" / f"{datetime.date.today().isoformat()}.json"   # 일별 스냅샷(향후 DB 적재용)
        snap.parent.mkdir(exist_ok=True)
        snap.write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    return out


def load(max_age_hours: float | None = None) -> dict | None:
    d = load_json(OUT)
    if d and max_age_hours is not None:
        age = datetime.datetime.now() - datetime.datetime.fromisoformat(d["generated_at"])
        if age.total_seconds() > max_age_hours * 3600:
            return None
    return d


def to_markdown(d: dict | None = None) -> str:
    d = d or load()
    if not d:
        return "## 정량 분석\n(아직 계산 전 — python quant.py)"
    L = [f"## 정량 분석 (CODE, quant.py · 계산 {d['generated_at'][:16]} · 가격 {d['price_last']})", "",
         "예상손익은 ① 변동성 기준(EWMA·추세 없음) ② 국면 기준(비슷한 과거 날짜의 실제 이후 경로). 분위수는 5/50/95%. 매매 신호 아님.", "",
         "| 포지션 | 기술 점수 | 6M ①중앙(5%~95%) 억 | ①손절 먼저 | 6M ②중앙(5%~95%) 억 | ②손절 먼저 | ②표본·플래그 |", "|---|---|---|---|---|---|---|"]
    for p in d["positions"]:
        v, a, t = p["vol"].get("6M") or {}, p["analog"].get("6M") or {}, p.get("tech") or {}
        qv, qa = v.get("q_bn") or [None] * 5, a.get("q_bn") or [None] * 5
        L.append(f"| {p['name']} | {t.get('score', '-')} {t.get('label', '')} | {qv[2]} ({qv[0]}~{qv[4]}) | {v.get('p_stop', '-')}% | "
                 f"{qa[2]} ({qa[0]}~{qa[4]}) | {a.get('p_stop', '-')}% | {a.get('n', 0)} {','.join(a.get('flags') or [])} |")
    pf = d["portfolio"]
    L += ["", f"**포트 6개월 (손실한도 {pf['limit_bn']:.0f}억)**: ① 중앙 {pf['vol']['q_bn'][2]}억, 5% {pf['vol']['q_bn'][0]}억, 기간 중 한도 초과 확률 {pf['vol']['p_breach']}% · "
          f"② 중앙 {(pf['analog'].get('q_bn') or [None]*5)[2]}억, 5% {(pf['analog'].get('q_bn') or [None]*5)[0]}억, 한도 초과 {pf['analog'].get('p_breach', '-')}% (표본 {pf['analog'].get('n', 0)})",
          "위험 기여 상위: " + ", ".join(f"{x['id']} {x['pct']}%" for x in pf.get("risk_contrib", [])[:5]), "",
          "**기회 스캐너 (기술 점수 순)**: " + "; ".join(f"{s['name']} {s['score']}({s['label']})" for s in d["scanner"][:8])]
    return "\n".join(L)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    res = run(full="--full" in sys.argv)   # --full: 이력 전체를 다시 받는다
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str) if "--json" in sys.argv else to_markdown(res))
