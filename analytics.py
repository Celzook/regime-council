# -*- coding: utf-8 -*-
"""시장 분석 지표 — BOND·STOCK·FX 에이전트가 공통으로 쓰는 결정적 계산 (LLM 개입 없음).

README 의 원칙("수치는 코드, 해석은 에이전트")을 시장 전반 분석으로 넓힌 모듈.
- Claude Code 대화형 에이전트(.claude/agents/{bond,stock,fx}.md)는 CLI 출력을 읽고 해석한다.
- API 에이전트(agent.py)는 to_markdown("all") 을 공통 컨텍스트로 받는다.

입력: data/history/{kofia,yf,fred}_daily.csv (fetch_history.py). market.json 이 더 최신이면 그 값을 끝에 잇는다.
      data/manual_inputs.json (스왑포인트·밸류에이션 등 수동 지표, 선택 — sample 값은 쓰지 않는다)
      data/positions.json (없으면 positions.sample.json = 전략 문서 계획치) — 포트 민감도
사용법:
  python analytics.py status                      데이터 기준일·갱신 필요 여부
  python analytics.py bond|equity|fx|cross|all    지표 표(마크다운). --json 이면 원자료
  python analytics.py calc <항목> ...             즉석 계산 (python analytics.py calc -h)
"""
import argparse
import datetime
import json
import math
import sys

import numpy as np
import pandas as pd

import config
from agent import load_json

HIST_DIR = config.DATA_DIR / "history"
HIST_FILES = ("kofia_daily.csv", "yf_daily.csv", "fred_daily.csv")
PANEL_START = "2000-01-01"
PCT_YEARS = 5

# 열 → (표시명, 종류, 표시배율, 단위, 출처)
# 종류: rate = 금리·스프레드(변화 bp) / price = 지수·환율·원자재(변화 %) / point = VIX(변화 pt)
SERIES = {
    "ktb_2y": ("국고 2년", "rate", 1, "%", "Refinitiv KOFIA 최종호가"),
    "ktb_3y": ("국고 3년", "rate", 1, "%", "Refinitiv KOFIA 최종호가"),
    "ktb_5y": ("국고 5년", "rate", 1, "%", "Refinitiv KOFIA 최종호가"),
    "ktb_10y": ("국고 10년", "rate", 1, "%", "Refinitiv KOFIA 최종호가"),
    "corp_aa_minus_3y": ("회사채 AA- 3년", "rate", 1, "%", "Refinitiv KOFIA 최종호가"),
    "ust_2y": ("미국채 2년", "rate", 1, "%", "Refinitiv US2YT=RR"),
    "ust_10y_cmt": ("미국채 10년", "rate", 1, "%", "Refinitiv US10YT=RR"),
    "fed_funds": ("연준 목표금리(상단)", "rate", 1, "%", "Refinitiv USFFTARGET="),
    "be_10y": ("미국 10년 BEI", "rate", 1, "%", "Refinitiv 명목−TIPS"),
    "us_ig_oas": ("미국 IG OAS", "rate", 100, "bp", "FRED ICE BofA"),
    "us_hy_oas": ("미국 HY OAS", "rate", 100, "bp", "FRED ICE BofA"),
    "us_ig_yield": ("미국 IG 유효수익률", "rate", 1, "%", "FRED ICE BofA"),
    "us_hy_yield": ("미국 HY 유효수익률", "rate", 1, "%", "FRED ICE BofA"),
    "usdkrw": ("USD/KRW", "price", 1, "원", "Refinitiv"),
    "dxy": ("달러인덱스", "price", 1, "pt", "Refinitiv"),
    "usdjpy": ("USD/JPY", "price", 1, "엔", "Refinitiv"),
    "eurusd": ("EUR/USD", "price", 1, "달러", "Refinitiv"),
    "usdcny": ("USD/CNY", "price", 1, "위안", "Refinitiv"),
    "kospi": ("KOSPI", "price", 1, "pt", "Refinitiv"),
    "kosdaq": ("KOSDAQ", "price", 1, "pt", "Refinitiv"),
    "sp500": ("S&P500", "price", 1, "pt", "Refinitiv"),
    "nasdaq": ("나스닥", "price", 1, "pt", "Refinitiv"),
    "nikkei": ("닛케이225", "price", 1, "pt", "Refinitiv"),
    "stoxx50": ("유로스톡스50", "price", 1, "pt", "Refinitiv"),
    "eem": ("MSCI EM(EEM ETF)", "price", 1, "달러", "Refinitiv"),
    "vix": ("VIX", "point", 1, "pt", "yfinance(Refinitiv 권한 없음)"),
    "wti": ("WTI", "price", 1, "달러/bbl", "Refinitiv"),
    "gold": ("금", "price", 1, "달러/oz", "Refinitiv"),
}
# 파생 시계열: 열 → (표시명, a, b) = a − b. 전부 rate(bp 표시).
DERIVED = {
    "spread_aa": ("AA- 스프레드(3년)", "corp_aa_minus_3y", "ktb_3y"),
    "ktb_3s10s": ("국고 3s10s", "ktb_10y", "ktb_3y"),
    "ktb_2s10s": ("국고 2s10s", "ktb_10y", "ktb_2y"),
    "ust_2s10s": ("미국채 2s10s", "ust_10y_cmt", "ust_2y"),
    "kr_us_2y": ("한미 2년 금리차(한−미)", "ktb_2y", "ust_2y"),
    "kr_us_10y": ("한미 10년 금리차(한−미)", "ktb_10y", "ust_10y_cmt"),
    "us_real_10y": ("미국 10년 실질금리(근사: 명목−BEI)", "ust_10y_cmt", "be_10y"),
}
for _k, (_name, _a, _b) in DERIVED.items():
    _bp = _k != "us_real_10y"
    SERIES[_k] = (_name, "rate", 100 if _bp else 1, "bp" if _bp else "%", f"계산({_a} − {_b})")

# market.json 최신값 → 시계열 열 (섹션, 키, 나눗수)
MARKET_MAP = {
    "ktb_2y": ("rates_kr", "ktb_2y", 1), "ktb_3y": ("rates_kr", "ktb_3y", 1), "ktb_5y": ("rates_kr", "ktb_5y", 1),
    "ktb_10y": ("rates_kr", "ktb_10y", 1), "corp_aa_minus_3y": ("rates_kr", "corp_aa_minus_3y", 1),
    "ust_2y": ("rates_us", "ust_2y", 1), "ust_10y_cmt": ("rates_us", "ust_10y", 1),
    "fed_funds": ("rates_us", "fed_funds", 1), "be_10y": ("rates_us", "breakeven_10y", 1),
    "us_ig_oas": ("credit_us", "ig_oas_bp", 100), "us_hy_oas": ("credit_us", "hy_oas_bp", 100),
    "us_ig_yield": ("credit_us", "ig_yield", 1), "us_hy_yield": ("credit_us", "hy_yield", 1),
    "usdkrw": ("fx", "usdkrw", 1), "dxy": ("fx", "dollar_index", 1), "usdjpy": ("fx", "usdjpy", 1),
    "eurusd": ("fx", "eurusd", 1), "usdcny": ("fx", "usdcny", 1),
    "kospi": ("equity", "kospi", 1), "kosdaq": ("equity", "kosdaq", 1), "sp500": ("equity", "sp500", 1),
    "nasdaq": ("equity", "nasdaq", 1), "nikkei": ("equity", "nikkei", 1), "stoxx50": ("equity", "stoxx50", 1),
    "eem": ("equity", "eem", 1), "vix": ("equity", "vix", 1),
    "wti": ("commodities", "wti", 1), "gold": ("commodities", "gold", 1),
}

GROUPS = {
    "rates_kr": ["ktb_2y", "ktb_3y", "ktb_5y", "ktb_10y", "corp_aa_minus_3y", "spread_aa", "ktb_3s10s", "ktb_2s10s"],
    "rates_us": ["fed_funds", "ust_2y", "ust_10y_cmt", "ust_2s10s", "be_10y", "us_real_10y", "kr_us_2y", "kr_us_10y"],
    "credit_us": ["us_ig_oas", "us_hy_oas", "us_ig_yield", "us_hy_yield"],
    "equity": ["kospi", "kosdaq", "sp500", "nasdaq", "nikkei", "stoxx50", "eem"],
    "fx": ["usdkrw", "dxy", "usdjpy", "eurusd", "usdcny"],
    "commodities": ["wti", "gold"],
}

# 주목 신호 규칙 (코드 판정, 관례적 임계 — 투자위 확정치 아님): (열, 지표, 임계, 방향, 자산)
SIGNAL_RULES = [
    ("ktb_3y", "chg_1m", 30, "abs", "bond"), ("ktb_10y", "chg_1m", 30, "abs", "bond"),
    ("ust_10y_cmt", "chg_1m", 30, "abs", "bond"),
    ("spread_aa", "chg_1m", 15, "up", "bond"), ("us_ig_oas", "chg_1m", 25, "up", "bond"),
    ("us_hy_oas", "chg_1m", 75, "up", "bond"),
    ("kospi", "chg_1m", 10, "abs", "equity"), ("kosdaq", "chg_1m", 10, "abs", "equity"),
    ("sp500", "chg_1m", 8, "abs", "equity"), ("nasdaq", "chg_1m", 10, "abs", "equity"),
    ("kospi", "dd_52w", -15, "below", "equity"), ("sp500", "dd_52w", -10, "below", "equity"),
    ("vix", "value", 25, "above", "equity"),
    ("usdkrw", "chg_1m", 3, "abs", "fx"), ("usdjpy", "chg_1m", 4, "abs", "fx"), ("dxy", "chg_1m", 3, "abs", "fx"),
    ("wti", "chg_1m", 15, "abs", "cross"), ("gold", "chg_1m", 10, "abs", "cross"),
]
EXTREME_PCTILE = {"ktb_3y": "bond", "ktb_10y": "bond", "spread_aa": "bond", "ust_10y_cmt": "bond",
                  "us_hy_oas": "bond", "usdkrw": "fx", "vix": "equity", "kr_us_10y": "fx"}
# 데이터 확인 규칙: 최근 1년 일간 변동이 이 폭(%)을 넘으면 실제 급변인지 데이터 오류인지 확인하라고 표시
DAILY_JUMP_CHECK = {"kospi": 8, "kosdaq": 8, "sp500": 6, "nasdaq": 7, "nikkei": 8, "stoxx50": 7, "eem": 8,
                    "usdkrw": 3, "dxy": 2.5, "usdjpy": 4, "eurusd": 3, "usdcny": 2}

ASSET_KO = {"bond": "채권", "equity": "주식", "fx": "FX", "cross": "교차자산"}
_CACHE: dict = {}


# ── 데이터 ───────────────────────────────────────────────────────
def _market() -> dict:
    return load_json(config.DATA_DIR / "market.json") or {}


def load_panel() -> pd.DataFrame:
    """세 CSV 를 날짜로 합치고, market.json 이 더 최신인 값을 끝에 잇고, 파생 시계열을 붙인다."""
    paths = [HIST_DIR / f for f in HIST_FILES]
    mpath = config.DATA_DIR / "market.json"
    key = tuple((p.name, p.stat().st_mtime) for p in paths + [mpath] if p.exists())
    if _CACHE.get("key") == key:
        return _CACHE["df"]
    frames = [pd.read_csv(p, index_col=0, parse_dates=True) for p in paths if p.exists()]
    if not frames:
        raise RuntimeError("data/history/ 에 시계열이 없습니다 — python fetch_history.py 를 먼저 실행하세요.")
    df = pd.concat(frames, axis=1, sort=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index >= PANEL_START]
    m = _market()
    for col, (sec, k, div) in MARKET_MAP.items():
        f = (m.get(sec) or {}).get(k)
        if not isinstance(f, dict) or f.get("value") is None or not f.get("as_of"):
            continue
        d = pd.Timestamp(f["as_of"])
        last = df[col].last_valid_index() if col in df else None
        if last is None or d > last:
            df.loc[d, col] = float(f["value"]) / div
    df = df.sort_index()
    filled = df.ffill(limit=3)  # 한미 휴장일 불일치만 메운다
    for k, (_n, a, b) in DERIVED.items():
        if a in df and b in df:
            both = df[a].notna() | df[b].notna()
            df[k] = (filled[a] - filled[b]).where(both)
    _CACHE.update(key=key, df=df)
    return df


def _round(v, unit):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    if unit == "%":
        return round(v, 3)
    if unit == "bp":
        return round(v, 1)
    return round(v, 4) if abs(v) < 10 else round(v, 2)


def stats(col: str, df: pd.DataFrame | None = None) -> dict | None:
    """한 시계열의 최신값·변화·52주 범위·5년 백분위·1년 z·(가격) 낙폭·변동성."""
    df = load_panel() if df is None else df
    if col not in df:
        return None
    name, kind, vs, unit, src = SERIES[col]
    s = df[col].dropna()
    if s.empty:
        return None
    d0, v = s.index[-1], float(s.iloc[-1])

    def base(off):
        b = s[s.index <= d0 - off]
        return float(b.iloc[-1]) if len(b) else None

    def chg(b):
        if b is None:
            return None
        if kind == "price":
            return round((v / b - 1) * 100, 2) if b else None
        if kind == "rate":
            return round((v - b) * 100, 1)
        return round(v - b, 2)

    w52 = s[s.index > d0 - pd.DateOffset(years=1)]
    win = s[s.index > d0 - pd.DateOffset(years=PCT_YEARS)]
    ytd = s[s.index < pd.Timestamp(d0.year, 1, 1)]
    short = win.index[0] > d0 - pd.DateOffset(years=PCT_YEARS) + pd.DateOffset(days=45)
    sd = float(w52.std()) if len(w52) > 20 else 0.0
    out = {
        "key": col, "name": name, "kind": kind, "unit": unit, "source": src,
        "value": _round(v * vs, unit), "as_of": d0.date().isoformat(),
        "chg_1w": chg(base(pd.DateOffset(weeks=1))), "chg_1m": chg(base(pd.DateOffset(months=1))),
        "chg_3m": chg(base(pd.DateOffset(months=3))), "chg_ytd": chg(float(ytd.iloc[-1]) if len(ytd) else None),
        "chg_1y": chg(base(pd.DateOffset(years=1))),
        "lo_52w": _round(float(w52.min()) * vs, unit), "hi_52w": _round(float(w52.max()) * vs, unit),
        "pctile": round(float((win <= v).mean() * 100)) if len(win) > 60 else None,
        "pctile_window": (f"{win.index[0].date()}~" if short else f"{PCT_YEARS}년"),
        "z_1y": round((v - float(w52.mean())) / sd, 2) if sd > 0 else None,
        "today": d0.date() == datetime.date.today(),
    }
    if kind == "price":
        r = s.pct_change().dropna()
        r1y = r[r.index > d0 - pd.DateOffset(years=1)]
        if len(r1y):
            out["max_daily_1y"] = {"pct": round(float(r1y.loc[r1y.abs().idxmax()]) * 100, 1),
                                   "date": r1y.abs().idxmax().date().isoformat()}
        out["dd_52w"] = round((v / float(w52.max()) - 1) * 100, 2) if len(w52) else None
        out["vol_20d"] = round(float(r.tail(20).std()) * math.sqrt(252) * 100, 1) if len(r) >= 20 else None
        out["vol_60d"] = round(float(r.tail(60).std()) * math.sqrt(252) * 100, 1) if len(r) >= 60 else None
    return out


def latest(col: str, df: pd.DataFrame | None = None) -> tuple[float, str] | None:
    """원 단위(배율 미적용) 최신값과 기준일."""
    df = load_panel() if df is None else df
    if col not in df:
        return None
    s = df[col].dropna()
    return (float(s.iloc[-1]), s.index[-1].date().isoformat()) if len(s) else None


def manual_inputs() -> dict:
    """data/manual_inputs.json 만 사용 — sample 의 예시값은 사실로 쓰지 않는다.
    인포맥스 브리지에 스왑포인트가 있고 외부 전송이 허용(config.SHARE_VENDOR_DATA)됐으면 그 실측값을 우선한다
    (이 함수 결과는 에이전트 컨텍스트로 나가므로 허용 전에는 쓰지 않는다)."""
    mi = dict(load_json(config.DATA_DIR / "manual_inputs.json") or {})
    if config.SHARE_VENDOR_DATA:
        live = load_json(config.DATA_DIR / "macro_live.json") or {}
        sw = next((s for s in live.get("series", []) if s.get("key") == "swap_3m" and s.get("status") == "OK"), None)
        if sw:
            mi["swap_point_3m"] = {"value": sw["value"], "unit": "KRW", "as_of": sw.get("date") or live["meta"]["read_at"][:10],
                                   "source": "인포맥스 실시간(브리지)"}
            mi.pop("hedge_cost_3m_annualized", None)
    return mi


# ── 계산식 (calc CLI 와 공유) ──────────────────────────────────────
def mod_duration(years: float, y_pct: float) -> float:
    """반기 이표 par 채권의 수정듀레이션 근사."""
    y = y_pct / 100
    if years <= 0:
        return 0.0
    if y <= 0:
        return years
    return (1 - (1 + y / 2) ** (-2 * years)) / y


def carry_roll(tenor: float, y: float, y_roll: float, horizon: float = 1.0) -> dict:
    """커브 불변 가정 horizon 보유 기대수익 = 이자(y×h) + 롤다운(D_after × (y − y_roll)). 손익분기 = 기대수익 / D_after."""
    d_after = mod_duration(tenor - horizon, y_roll)
    roll = d_after * (y - y_roll)
    total = y * horizon + roll
    return {"tenor": tenor, "yield": round(y, 3), "roll_to": tenor - horizon, "roll_yield": round(y_roll, 3),
            "carry_pct": round(y * horizon, 3), "rolldown_pct": round(roll, 3), "total_pct": round(total, 3),
            "mod_dur_now": round(mod_duration(tenor, y), 2), "mod_dur_after": round(d_after, 2),
            "breakeven_bp": round(total / d_after * 100, 1) if d_after else None}


def interp(points: dict[float, float], t: float) -> float:
    ks = sorted(points)
    if t <= ks[0]:
        return points[ks[0]]
    if t >= ks[-1]:
        return points[ks[-1]]
    for a, b in zip(ks, ks[1:]):
        if a <= t <= b:
            return points[a] + (points[b] - points[a]) * (t - a) / (b - a)
    return points[ks[-1]]


def hedge_cost_from_swap(swap_points: float, spot: float, days: int = 91) -> float:
    """연환산 헤지비용(%p, +면 비용) = −스왑포인트/현물 × 365/일수. USD 매도 선물환(해외자산 헤지) 기준."""
    return -swap_points / spot * 365 / days * 100


def bond_pnl(mv_bn: float, duration: float, dy_bp: float, convexity: float = 0.0) -> float:
    """금리 변화 손익(억) ≈ −D×Δy + ½×C×Δy²."""
    dy = dy_bp / 10000
    return mv_bn * (-duration * dy + 0.5 * convexity * dy * dy)


# ── 포트 민감도 (positions → limits.expand_legs 재사용) ─────────────────
def portfolio_sensitivity() -> dict:
    import limits

    doc = limits.load_positions()
    legs = limits.expand_legs(doc.get("positions", []), [])
    bond = [g for g in legs if g["asset"] == "bond"]
    eq = [g for g in legs if g["asset"] == "equity"]
    fx = [g for g in legs if g["asset"] == "fx"]
    dv01 = sum(g["mv_bn"] * g.get("duration", 0) for g in bond) / 1e4
    dv01_scope = sum(g["mv_bn"] * g.get("duration", 0) for g in bond if g.get("rate_in_scope", True)) / 1e4
    cs01 = sum(g["mv_bn"] * g.get("spread_duration", 0) * g.get("spread_beta", 1.0) for g in bond) / 1e4
    expo = sum(g.get("exposure_bn", 0) for g in fx)
    open_bn = sum(g["mv_bn"] for g in fx)
    return {
        "source": "positions.json" if not limits.is_sample("positions") else "positions.sample.json(전략 문서 계획치 — 실제 보유 아님)",
        "as_of": doc.get("meta", {}).get("as_of"),
        "bond_mv_bn": round(sum(g["mv_bn"] for g in bond), 1),
        "bond_duration": round(sum(g["mv_bn"] * g.get("duration", 0) for g in bond) / max(1e-9, sum(g["mv_bn"] for g in bond)), 2) if bond else None,
        "dv01_bn": round(dv01, 3), "dv01_in_scope_bn": round(dv01_scope, 3), "cs01_beta_bn": round(cs01, 3),
        "equity_mv_bn": round(sum(g["mv_bn"] for g in eq), 1),
        "equity_beta_exposure_bn": round(sum(g["mv_bn"] * g.get("beta", 1.0) for g in eq), 1),
        "fx_exposure_bn": round(expo, 1), "fx_open_bn": round(open_bn, 1),
        "fx_open_ratio_pct": round(open_bn / expo * 100, 1) if expo else None,
        "legs_estimated": [g["name"] for g in legs if g.get("estimated")],
    }


# ── 자산별 분석 ─────────────────────────────────────────────────────
def bond_view(df: pd.DataFrame) -> dict:
    out = {"rates_kr": [stats(c, df) for c in GROUPS["rates_kr"]],
           "rates_us": [stats(c, df) for c in GROUPS["rates_us"]],
           "credit_us": [stats(c, df) for c in GROUPS["credit_us"]]}
    pts = {t: latest(c, df) for t, c in ((2, "ktb_2y"), (3, "ktb_3y"), (5, "ktb_5y"), (10, "ktb_10y"))}
    curve = {t: v[0] for t, v in pts.items() if v}
    rows = []
    if len(curve) >= 3:
        for t in (3, 5, 10):
            if t in curve:
                r = carry_roll(t, curve[t], interp(curve, t - 1))
                r["name"] = f"국고 {t}년"
                r["dv01_per_100bn_mn"] = round(mod_duration(t, curve[t]), 2)  # 100억당 1bp = 수정듀레이션 × 100만원
                rows.append(r)
        aa = latest("corp_aa_minus_3y", df)
        if aa and 2 in curve and 3 in curve:
            r = carry_roll(3, aa[0], aa[0] - (curve[3] - curve[2]))
            r["name"] = "회사채 AA- 3년(국고 커브 평행 가정)"
            r["dv01_per_100bn_mn"] = round(mod_duration(3, aa[0]), 2)
            rows.append(r)
    out["carry_roll"] = {"as_of": (pts.get(3) or (None, None))[1], "rows": rows,
                         "assumption": "1년 보유, 커브 불변, 반기 이표 par 채권 근사. 손익분기 = 1년 기대수익이 0 이 되는 금리 상승폭."}
    try:
        out["portfolio"] = portfolio_sensitivity()
    except Exception as e:  # noqa: BLE001 — 포지션 파일 문제로 시장 분석까지 죽지 않게
        out["portfolio"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def equity_view(df: pd.DataFrame) -> dict:
    out = {"indices": [stats(c, df) for c in GROUPS["equity"]], "vix": stats("vix", df)}
    k, s, u = df.get("kospi"), df.get("sp500"), df.get("usdkrw")
    rel = {}
    if k is not None and s is not None:
        ks, ss = stats("kospi", df), stats("sp500", df)
        if ks and ss and ks["chg_3m"] is not None and ss["chg_3m"] is not None:
            rel["kospi_minus_sp500_3m_pp"] = round(ks["chg_3m"] - ss["chg_3m"], 2)
    if k is not None and u is not None:
        both = pd.concat([k, u], axis=1).ffill(limit=3).dropna()
        if len(both) > 70:
            usd = both.iloc[:, 0] / both.iloc[:, 1]
            d0 = usd.index[-1]
            b = usd[usd.index <= d0 - pd.DateOffset(months=3)]
            if len(b):
                rel["kospi_usd_3m_pct"] = round((usd.iloc[-1] / b.iloc[-1] - 1) * 100, 2)
    out["relative"] = rel
    mi = manual_inputs()
    val = []
    for idx, per_key, rf_col in (("KOSPI", "kospi_per_fwd", "ktb_10y"), ("S&P500", "sp500_per_fwd", "ust_10y_cmt")):
        f = mi.get(per_key)
        rf = latest(rf_col, df)
        if isinstance(f, dict) and f.get("value") and rf:
            ey = 100 / float(f["value"])
            val.append({"index": idx, "per": f["value"], "per_as_of": f.get("as_of"), "per_source": f.get("source"),
                        "earnings_yield_pct": round(ey, 2), "rf_key": rf_col, "rf_pct": round(rf[0], 3), "rf_as_of": rf[1],
                        "yield_gap_pp": round(ey - rf[0], 2)})
    out["valuation"] = val
    k3 = latest("ktb_3y", df)
    out["dividend_vs_ktb"] = {"ktb_3y": round(k3[0], 3) if k3 else None, "as_of": k3[1] if k3 else None}
    dy = mi.get("kospi_div_yield")
    if isinstance(dy, dict) and dy.get("value") is not None and k3:
        out["dividend_vs_ktb"]["kospi_div_yield"] = dy["value"]
        out["dividend_vs_ktb"]["kospi_div_minus_ktb3y_bp"] = round((float(dy["value"]) - k3[0]) * 100, 1)
    try:
        out["portfolio"] = portfolio_sensitivity()
    except Exception as e:  # noqa: BLE001
        out["portfolio"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def fx_view(df: pd.DataFrame) -> dict:
    out = {"pairs": [stats(c, df) for c in GROUPS["fx"]],
           "rate_diff": [stats(c, df) for c in ("kr_us_2y", "kr_us_10y")]}
    uk, dx, cn = stats("usdkrw", df), stats("dxy", df), stats("usdcny", df)
    idio = {}
    if uk and dx and uk["chg_3m"] is not None and dx["chg_3m"] is not None:
        idio["usdkrw_minus_dxy_3m_pp"] = round(uk["chg_3m"] - dx["chg_3m"], 2)
    if uk and cn and uk["chg_3m"] is not None and cn["chg_3m"] is not None:
        idio["usdkrw_minus_usdcny_3m_pp"] = round(uk["chg_3m"] - cn["chg_3m"], 2)
    out["krw_idiosyncratic"] = idio

    # 헤지비용: 수동 스왑포인트(실측) / 2년 금리차 근사 / 전략 가정
    hc = []
    mi = manual_inputs()
    spot = latest("usdkrw", df)
    if isinstance(mi.get("hedge_cost_3m_annualized"), dict):
        f = mi["hedge_cost_3m_annualized"]
        hc.append({"basis": "수동 입력 3개월 헤지비용", "pct": float(f["value"]), "as_of": f.get("as_of"), "tag": "F"})
    elif isinstance(mi.get("swap_point_3m"), dict) and spot:
        f = mi["swap_point_3m"]
        hc.append({"basis": f"수동 스왑포인트 3M {f['value']}원 ÷ 현물 {round(spot[0], 2)} × 365/91",
                   "pct": round(hedge_cost_from_swap(float(f["value"]), spot[0]), 3), "as_of": f.get("as_of"), "tag": "E"})
    u2, k2 = latest("ust_2y", df), latest("ktb_2y", df)
    if u2 and k2:
        hc.append({"basis": "2년 금리차 근사(미국채2y − 국고2y, 커버드 금리평가)", "pct": round(u2[0] - k2[0], 3),
                   "as_of": min(u2[1], k2[1]), "tag": "E"})
    hc.append({"basis": "전략 문서 가정", "pct": config.STRATEGY["hedge_cost_assumption_pct"], "as_of": None, "tag": "A"})
    out["hedge_cost"] = hc

    # 헤지 후 수익률: 미국 IG·HY 유효수익률 − 헤지비용 vs 국내 대안
    ig, hy = latest("us_ig_yield", df), latest("us_hy_yield", df)
    aa, k3 = latest("corp_aa_minus_3y", df), latest("ktb_3y", df)
    hedged = []
    for h in hc:
        row = {"hedge_basis": h["basis"], "hedge_cost_pct": h["pct"], "tag": h["tag"]}
        if ig:
            row["us_ig_hedged_pct"] = round(ig[0] - h["pct"], 3)
        if hy:
            row["us_hy_hedged_pct"] = round(hy[0] - h["pct"], 3)
        hedged.append(row)
    out["hedged_yield"] = {"rows": hedged,
                           "us_ig_yield": (round(ig[0], 3), ig[1]) if ig else None,
                           "us_hy_yield": (round(hy[0], 3), hy[1]) if hy else None,
                           "kr_aa_3y": (round(aa[0], 3), aa[1]) if aa else None,
                           "ktb_3y": (round(k3[0], 3), k3[1]) if k3 else None,
                           "note": "유효수익률은 지수 듀레이션 기준(만기·듀레이션이 국내 3년물과 다름) — 수준 비교용 근사."}

    # 환오픈 손익 표
    try:
        ps = portfolio_sensitivity()
    except Exception as e:  # noqa: BLE001
        ps = {"error": f"{type(e).__name__}: {e}"}
    out["portfolio"] = ps
    shocks = [-10, -5, -3, 3, 5, 10]
    rows = []
    cap = config.STRATEGY["fx_overlay_limit_bn"]
    cases = [("현재 환오픈", ps.get("fx_open_bn"))] if ps.get("fx_open_bn") else []
    cases += [(f"오버레이 한도 {int(p * 100)}%", round(cap * p, 1)) for p in (0.5, 1.0)]
    sig = (uk or {}).get("vol_60d")
    for label, open_bn in cases:
        r = {"case": label, "open_bn": open_bn, "pnl_by_shock_bn": {f"{s:+d}%": round(open_bn * s / 100, 1) for s in shocks}}
        if sig:
            r["pnl_1sigma_month_bn"] = round(open_bn * sig / math.sqrt(12) / 100, 1)
        rows.append(r)
    out["open_shock"] = {"rows": rows, "usdkrw_vol_60d": sig,
                         "sign": "+ = 원화 약세(USD/KRW 상승) → 환오픈(달러 롱) 이익, − = 원화 강세 → 손실"}
    return out


def _corr(a: pd.Series, b: pd.Series, n: int) -> float | None:
    j = pd.concat([a, b], axis=1).dropna().tail(n)
    if len(j) < max(20, n // 2):
        return None
    return round(float(j.iloc[:, 0].corr(j.iloc[:, 1])), 2)


def cross_view(df: pd.DataFrame) -> dict:
    f = df.ffill(limit=3)
    ret = lambda c: f[c].pct_change(fill_method=None) if c in f else pd.Series(dtype=float)  # noqa: E731
    dif = lambda c: f[c].diff() if c in f else pd.Series(dtype=float)  # noqa: E731
    pairs = [
        ("KOSPI 수익률 vs 국고3y 변화", ret("kospi"), dif("ktb_3y"), "stock_bond"),
        ("S&P500 수익률 vs 미국채10y 변화", ret("sp500"), dif("ust_10y_cmt"), "stock_bond"),
        ("KOSPI 수익률 vs USD/KRW 변화율", ret("kospi"), ret("usdkrw"), "stock_fx"),
        ("S&P500 수익률 vs USD/KRW 변화율", ret("sp500"), ret("usdkrw"), "stock_fx"),
    ]
    rows = []
    for name, a, b, typ in pairs:
        c60, c252 = _corr(a, b, 60), _corr(a, b, 252)
        rows.append({"pair": name, "corr_60d": c60, "corr_1y": c252, "reading": _corr_reading(c60, typ)})
    return {"correlations": rows, "commodities": [stats(c, df) for c in GROUPS["commodities"]],
            "note": "일별 변화의 상관. 한국·미국 시장 마감 시각 차이로 동일 날짜 비교는 근사."}


def _corr_reading(c: float | None, typ: str) -> str:
    if c is None:
        return "표본 부족"
    if typ == "stock_bond":
        if c > 0.2:
            return "주가↓·금리↓ 동행 — 채권이 주식 손실을 일부 상쇄하는 국면"
        if c < -0.2:
            return "주가↓·금리↑ 동행 — 주식·채권 동반 손실 위험(분산 효과 약화)"
        return "뚜렷한 동행 없음"
    if c < -0.2:
        return "주가↓·원화 약세 동행 — 환오픈(달러 롱)이 주식 손실을 상쇄"
    if c > 0.2:
        return "주가↓·원화 강세 동행 — 환오픈이 주식 손실을 키움(이례적)"
    return "뚜렷한 동행 없음"


def signals(df: pd.DataFrame) -> list[dict]:
    out = []
    cache = {}
    for col, metric, thr, mode, asset in SIGNAL_RULES:
        st = cache.setdefault(col, stats(col, df))
        if not st or st.get(metric) is None:
            continue
        v = st[metric]
        hit = (mode == "abs" and abs(v) >= thr) or (mode == "up" and v >= thr) or \
              (mode == "below" and v <= thr) or (mode == "above" and v >= thr)
        if hit:
            unit = {"rate": "bp", "price": "%", "point": "pt"}[st["kind"]] if metric.startswith("chg") else \
                ("%" if metric == "dd_52w" else st["unit"])
            label = {"chg_1m": "1개월 변화", "dd_52w": "52주 고점 대비", "value": "수준"}[metric]
            thr_s = {"abs": f"±{thr:g}", "up": f"+{thr:g}", "below": f"{thr:g}", "above": f"{thr:g}"}[mode]
            out.append({"asset": asset, "key": col, "text": f"{st['name']} {label} {v:+g}{unit} (임계 {thr_s}{unit}, 기준일 {st['as_of']})"})
    for col, asset in EXTREME_PCTILE.items():
        st = cache.setdefault(col, stats(col, df))
        if st and st.get("pctile") is not None and (st["pctile"] >= 95 or st["pctile"] <= 5):
            out.append({"asset": asset, "key": col,
                        "text": f"{st['name']} {st['value']}{st['unit']} — {st['pctile_window']} 백분위 {st['pctile']}% (극단 구간, 기준일 {st['as_of']})"})
    for col, lim in DAILY_JUMP_CHECK.items():
        st = cache.setdefault(col, stats(col, df))
        mj = (st or {}).get("max_daily_1y")
        if mj and abs(mj["pct"]) >= lim:
            asset = "fx" if col in GROUPS["fx"] else "equity"
            out.append({"asset": asset, "key": col, "kind": "data_check",
                        "text": f"[데이터 확인] {st['name']} 최근 1년 일간 최대 변동 {mj['pct']:+g}%({mj['date']}, 확인 기준 ±{lim:g}%) "
                                "— 실제 급변인지 데이터 오류인지 확인 전에는 변동성·낙폭 수치를 단정적으로 쓰지 말 것"})
    return out


def status() -> dict:
    today = datetime.date.today()
    files = {}
    for name in HIST_FILES:
        p = HIST_DIR / name
        if p.exists():
            d = pd.read_csv(p, index_col=0, parse_dates=True).dropna(how="all")
            files[name] = d.index.max().date().isoformat() if len(d) else None
        else:
            files[name] = None
    m = _market()
    gen = (m.get("meta") or {}).get("generated_at")
    gen_date = gen[:10] if gen else None
    problems = []
    if not gen_date or gen_date < today.isoformat():
        problems.append(f"시장 스냅샷(market.json) 생성일 {gen_date or '없음'} — 오늘 것이 아님")
    k = files.get("kofia_daily.csv")
    if not k or (today - datetime.date.fromisoformat(k)).days > 4:
        problems.append(f"과거 시계열(KOFIA) 마지막 {k or '없음'} — 4일 넘게 지남")
    if not files.get("fred_daily.csv"):
        problems.append("fred_daily.csv 없음 — 미국 금리·크레딧 이력 미수집")
    import limits
    return {"today": today.isoformat(), "history_last": files, "market_generated_at": gen,
            "manual_inputs": sorted(manual_inputs().keys()) or None,
            "positions": "positions.json" if not limits.is_sample("positions") else "positions.sample.json(계획치)",
            "need_refresh": bool(problems), "problems": problems,
            "refinitiv": _refinitiv_status(),
            "refresh_cmd": "python main.py fetch && python fetch_history.py  (Workspace 로그인 · Windows 파이썬, WSL 에선 python.exe)"}


def _refinitiv_status() -> dict:
    try:
        import refinitiv_src
        refinitiv_src.available()
        return refinitiv_src.status()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def compute(section: str = "all") -> dict:
    df = load_panel()
    out = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"), "status": status()}
    sig = signals(df)
    if section in ("bond", "all"):
        out["bond"] = bond_view(df)
    if section in ("equity", "all"):
        out["equity"] = equity_view(df)
    if section in ("fx", "all"):
        out["fx"] = fx_view(df)
    if section in ("cross", "all"):
        out["cross"] = cross_view(df)
    out["signals"] = sig if section == "all" else [s for s in sig if s["asset"] in (section, "cross")]
    return out


# ── 마크다운 ───────────────────────────────────────────────────────
def _fmt(v, nd=None):
    if v is None:
        return "-"
    if isinstance(v, float) and nd is not None:
        return f"{v:+.{nd}f}"
    return str(v)


def _level_table(rows: list[dict | None]) -> list[str]:
    """금리·스프레드(변화 bp)와 VIX(변화 pt)용."""
    L = ["| 지표 (키) | 값 | 기준일 | 1주 | 1개월 | 3개월 | 1년 | 52주 범위 | 백분위(창) | 1년 z |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if not r:
            continue
        cu = {"rate": "bp", "price": "%", "point": "pt"}[r["kind"]]
        L.append(f"| {r['name']} ({r['key']}) | {r['value']}{r['unit']} | {r['as_of']}{' *' if r['today'] and r['kind'] != 'rate' else ''} | "
                 f"{_fmt(r['chg_1w'])}{cu} | {_fmt(r['chg_1m'])}{cu} | {_fmt(r['chg_3m'])}{cu} | {_fmt(r['chg_1y'])}{cu} | "
                 f"{r['lo_52w']}~{r['hi_52w']} | {_fmt(r['pctile'])}% ({r['pctile_window']}) | {_fmt(r['z_1y'])} |")
    return L


def _price_table(rows: list[dict | None]) -> list[str]:
    L = ["| 지표 (키) | 값 | 기준일 | 1주% | 1개월% | 3개월% | YTD% | 1년% | 52주 고점 대비% | 변동성 20일/60일(연율%) |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if not r:
            continue
        L.append(f"| {r['name']} ({r['key']}) | {r['value']} | {r['as_of']}{' *' if r['today'] else ''} | {_fmt(r['chg_1w'])} | "
                 f"{_fmt(r['chg_1m'])} | {_fmt(r['chg_3m'])} | {_fmt(r['chg_ytd'])} | {_fmt(r['chg_1y'])} | "
                 f"{_fmt(r.get('dd_52w'))} | {_fmt(r.get('vol_20d'))}/{_fmt(r.get('vol_60d'))} |")
    return L


def _portfolio_lines(ps: dict, asset: str) -> list[str]:
    if not ps or ps.get("error"):
        return [f"- 포트 민감도 계산 실패: {(ps or {}).get('error')}"]
    L = [f"출처 {ps['source']}, 기준일 {ps['as_of']}"]
    if asset == "bond":
        L += [f"- 채권 평가액 {ps['bond_mv_bn']}억 · 가중 듀레이션 {ps['bond_duration']}",
              f"- DV01(금리 1bp) {ps['dv01_bn']}억 — 손실한도 대상(rate_pnl_in_scope) 분 {ps['dv01_in_scope_bn']}억",
              f"- CS01(스프레드 1bp, 등급 β 가중) {ps['cs01_beta_bn']}억",
              "- 평행 이동 손익(억): " + ", ".join(f"{b:+d}bp {round(-ps['dv01_bn'] * b, 1):+g}" for b in (-50, -25, 25, 50, 100)),
              "- 스프레드 확대 손익(억, β 가중): " + ", ".join(f"+{b}bp {round(-ps['cs01_beta_bn'] * b, 1):+g}" for b in (25, 50, 100))]
    elif asset == "equity":
        L += [f"- 주식 평가액 {ps['equity_mv_bn']}억(위탁 look-through 포함) · 베타 가중 노출 {ps['equity_beta_exposure_bn']}억",
              "- 지수 변동 손익(억): " + ", ".join(f"{p:+d}% {round(ps['equity_beta_exposure_bn'] * p / 100, 1):+g}" for p in (-20, -10, -5, 5))]
    if ps.get("legs_estimated"):
        L.append(f"- 추정 분할 포함(위탁 손익 분리 미보고): {', '.join(ps['legs_estimated'])}")
    return L


def to_markdown(section: str = "all", res: dict | None = None) -> str:
    res = res or compute(section)
    st = res["status"]
    # 머리말에 생성 시각 대신 스냅샷 시각을 쓴다 — API 프롬프트 캐시 프리픽스가 호출마다 바뀌지 않게
    L = [f"## 시장 분석 지표 (CODE 계산, analytics.py · 시장 스냅샷 {st['market_generated_at']})", "",
         "수치는 전부 코드 계산 — 인용 시 '(analytics, 기준일)' 형식. 금리 변화는 bp, 가격 변화는 %. "
         "백분위 = 창 안에서 현재값 이하 비율. 기준일 옆 * = 당일 값(장중일 수 있음)."]
    if st["need_refresh"]:
        L.append("**데이터 신선도 주의**: " + " / ".join(st["problems"]))
    L += ["", "### 주목 신호 (코드 규칙, 관례적 임계)"]
    L += [f"- [{ASSET_KO[s['asset']]}] {s['text']}" for s in res["signals"]] or ["- 없음"]

    if "bond" in res:
        b = res["bond"]
        L += ["", "### 채권 — 국내 금리·스프레드"] + _level_table(b["rates_kr"])
        L += ["", "### 채권 — 미국 금리·한미 금리차"] + _level_table(b["rates_us"])
        L += ["", "### 채권 — 미국 크레딧 (ICE BofA, FRED 는 최근 3년만 제공 → 백분위 창 짧음)"] + _level_table(b["credit_us"])
        cr = b["carry_roll"]
        L += ["", f"### 채권 — 캐리·롤다운 (기준일 {cr['as_of']}, {cr['assumption']})",
              "| 구분 | 수익률% | 1년 후 만기→수익률% | 이자% | 롤다운% | 1년 기대수익% | 손익분기 금리상승(bp) | 수정듀레이션 | 100억당 1bp(백만원) |",
              "|---|---|---|---|---|---|---|---|---|"]
        for r in cr["rows"]:
            L.append(f"| {r['name']} | {r['yield']} | {r['roll_to']:g}년→{r['roll_yield']} | {r['carry_pct']} | {r['rolldown_pct']:+} | "
                     f"{r['total_pct']} | {r['breakeven_bp']} | {r['mod_dur_now']} | {r['dv01_per_100bn_mn']} |")
        L += ["", "### 채권 — 보유 포지션 민감도"] + _portfolio_lines(b.get("portfolio"), "bond")

    if "equity" in res:
        e = res["equity"]
        L += ["", "### 주식 — 지수"] + _price_table(e["indices"])
        L += ["", "### 주식 — 위험선호"] + _level_table([e["vix"]])
        rel = e["relative"]
        if rel:
            L.append("- 상대 성과: " + ", ".join(
                [f"KOSPI−S&P 3개월 {rel['kospi_minus_sp500_3m_pp']:+}%p" if "kospi_minus_sp500_3m_pp" in rel else "",
                 f"KOSPI 달러 환산 3개월 {rel['kospi_usd_3m_pct']:+}%(외국인 관점)" if "kospi_usd_3m_pct" in rel else ""]).strip(", "))
        L += ["", "### 주식 — 밸류에이션"]
        if e["valuation"]:
            for v in e["valuation"]:
                L.append(f"- {v['index']} 선행 PER {v['per']}배({v['per_source']}, {v['per_as_of']}) → 이익수익률 {v['earnings_yield_pct']}% − "
                         f"{v['rf_key']} {v['rf_pct']}%({v['rf_as_of']}) = 일드갭 {v['yield_gap_pp']:+}%p")
        else:
            L.append("- 데이터 없음 — data/manual_inputs.json 에 kospi_per_fwd / sp500_per_fwd(value·as_of·source) 입력 필요. "
                     "대화형 분석에서는 웹 검색으로 출처·날짜를 붙여 보완.")
        dv = e["dividend_vs_ktb"]
        L.append(f"- 배당: KOSPI 배당수익률 {dv['kospi_div_yield']}% − 국고3y {dv['ktb_3y']}%({dv['as_of']}) = {dv['kospi_div_minus_ktb3y_bp']}bp"
                 if "kospi_div_yield" in dv else "- 배당: KOSPI 배당수익률 데이터 없음 (manual_inputs.json 의 kospi_div_yield)")
        L += ["", "### 주식 — 보유 포지션 민감도"] + _portfolio_lines(e.get("portfolio"), "equity")

    if "fx" in res:
        f = res["fx"]
        L += ["", "### FX — 주요 환율"] + _price_table(f["pairs"])
        L += [""] + _level_table(f["rate_diff"])
        idio = f["krw_idiosyncratic"]
        if idio:
            L.append("- 원화 고유 요인(3개월): " + ", ".join(
                ([f"USD/KRW − DXY {idio['usdkrw_minus_dxy_3m_pp']:+}%p"] if "usdkrw_minus_dxy_3m_pp" in idio else []) +
                ([f"USD/KRW − USD/CNY {idio['usdkrw_minus_usdcny_3m_pp']:+}%p"] if "usdkrw_minus_usdcny_3m_pp" in idio else []))
                + " (+ 면 달러·위안 대비 원화가 더 약함)")
        L += ["", "### FX — 헤지비용과 헤지 후 수익률",
              "| 헤지비용 근거 | 태그 | 헤지비용(%p) | 기준일 | 미국 IG 헤지 후% | 미국 HY 헤지 후% |", "|---|---|---|---|---|---|"]
        for h, r in zip(f["hedge_cost"], f["hedged_yield"]["rows"]):
            L.append(f"| {h['basis']} | [{h['tag']}] | {h['pct']} | {h['as_of'] or '-'} | {r.get('us_ig_hedged_pct', '-')} | {r.get('us_hy_hedged_pct', '-')} |")
        hy = f["hedged_yield"]
        vd = lambda t: f"{t[0]}% ({t[1]})" if t else "데이터 없음"  # noqa: E731
        L.append(f"- 비교: 미국 IG {vd(hy['us_ig_yield'])}, 미국 HY {vd(hy['us_hy_yield'])}, "
                 f"국내 AA- 3y {vd(hy['kr_aa_3y'])}, 국고3y {vd(hy['ktb_3y'])}. {hy['note']}")
        if not any(h["basis"].startswith("수동") for h in f["hedge_cost"]):
            L.append("- 실측 헤지비용 없음 — data/manual_inputs.json 에 swap_point_3m(원) 또는 hedge_cost_3m_annualized(%p) 입력 필요.")
        os_ = f["open_shock"]
        ps = f.get("portfolio") or {}
        L += ["", f"### FX — 환오픈 손익 (억원, {os_['sign']})",
              f"해외 익스포저 {ps.get('fx_exposure_bn')}억 · 현재 환오픈 {ps.get('fx_open_bn')}억 · 오버레이 한도 {config.STRATEGY['fx_overlay_limit_bn']}억 · "
              f"출처 {ps.get('source')} · USD/KRW 60일 변동성 {os_['usdkrw_vol_60d']}%"]
        if os_["rows"]:
            ks = list(os_["rows"][0]["pnl_by_shock_bn"].keys())
            L += ["| 케이스 | 환오픈(억) | " + " | ".join(ks) + " | 월간 1σ |", "|---|---|" + "---|" * len(ks) + "---|"]
            for r in os_["rows"]:
                L.append(f"| {r['case']} | {r['open_bn']} | " + " | ".join(f"{v:+g}" for v in r["pnl_by_shock_bn"].values())
                         + f" | ±{r.get('pnl_1sigma_month_bn', '-')} |")

    if "cross" in res:
        c = res["cross"]
        L += ["", f"### 교차자산 — 상관 ({c['note']})", "| 짝 | 60일 | 1년 | 해석(코드 규칙) |", "|---|---|---|---|"]
        for r in c["correlations"]:
            L.append(f"| {r['pair']} | {_fmt(r['corr_60d'])} | {_fmt(r['corr_1y'])} | {r['reading']} |")
        L += ["", "### 교차자산 — 원자재"] + _price_table(c["commodities"])
    return "\n".join(L)


def status_markdown(st: dict) -> str:
    L = [f"오늘 {st['today']} · 시장 스냅샷 생성 {st['market_generated_at']} · 포지션 {st['positions']}",
         "과거 시계열 마지막 날짜: " + ", ".join(f"{k} {v}" for k, v in st["history_last"].items()),
         f"수동 입력: {', '.join(st['manual_inputs']) if st['manual_inputs'] else '없음(manual_inputs.json 미작성)'}",
         f"갱신 필요: {'예 — ' + ' / '.join(st['problems']) if st['need_refresh'] else '아니오'}"]
    if st["need_refresh"]:
        L.append(f"갱신 명령: {st['refresh_cmd']}")
    return "\n".join(L)


# ── CLI ──────────────────────────────────────────────────────────
def _calc(args) -> dict:
    if args.item == "carry":
        df = load_panel()
        if args.yield_ is None:
            col = {2: "ktb_2y", 3: "ktb_3y", 5: "ktb_5y", 10: "ktb_10y"}.get(int(args.tenor))
            if not col or not latest(col, df):
                raise SystemExit("--yield 를 주거나 국고 2·3·5·10년 중 하나를 고르세요.")
            args.yield_ = latest(col, df)[0]
        if args.roll_yield is None:
            curve = {t: latest(c, df)[0] for t, c in ((2, "ktb_2y"), (3, "ktb_3y"), (5, "ktb_5y"), (10, "ktb_10y")) if latest(c, df)}
            args.roll_yield = interp(curve, args.tenor - args.horizon)
        return carry_roll(args.tenor, args.yield_, args.roll_yield, args.horizon)
    if args.item == "hedge-cost":
        return {"hedge_cost_pct": round(hedge_cost_from_swap(args.swap_points, args.spot, args.days), 3),
                "formula": f"-({args.swap_points})/{args.spot} × 365/{args.days} × 100"}
    if args.item == "hedged-yield":
        hc = args.hedge_cost if args.hedge_cost is not None else config.STRATEGY["hedge_cost_assumption_pct"]
        return {"yield_pct": args.yield_, "hedge_cost_pct": hc, "hedged_yield_pct": round(args.yield_ - hc, 3),
                "hedge_cost_basis": "입력값" if args.hedge_cost is not None else "전략 문서 가정[A]"}
    if args.item == "fx-shock":
        open_bn = args.exposure * args.open_pct / 100
        return {"open_bn": round(open_bn, 1), "shock_pct": args.shock, "pnl_bn": round(open_bn * args.shock / 100, 2),
                "sign": "shock + = 원화 약세(이익), − = 원화 강세(손실)"}
    if args.item == "bond-pnl":
        return {"pnl_bn": round(bond_pnl(args.mv, args.duration, args.dy_bp, args.convexity), 3),
                "formula": "mv × (−D×Δy + ½×C×Δy²)"}
    if args.item == "erp":
        ey = 100 / args.per
        return {"earnings_yield_pct": round(ey, 3), "rf_pct": args.rf, "yield_gap_pp": round(ey - args.rf, 3)}
    raise SystemExit(f"알 수 없는 항목: {args.item}")


def main(argv=None):
    p = argparse.ArgumentParser(description="시장 분석 지표 (CODE 계산)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="데이터 기준일·갱신 필요 여부")
    for name in ("bond", "equity", "fx", "cross", "all"):
        sp = sub.add_parser(name, help=f"{name} 지표")
        sp.add_argument("--json", action="store_true", help="원자료 JSON 출력")
    c = sub.add_parser("calc", help="즉석 계산")
    c.add_argument("item", choices=["carry", "hedge-cost", "hedged-yield", "fx-shock", "bond-pnl", "erp"])
    c.add_argument("--tenor", type=float, help="carry: 만기(년)")
    c.add_argument("--yield", dest="yield_", type=float, help="carry/hedged-yield: 수익률(%%)")
    c.add_argument("--roll-yield", type=float, help="carry: 1년 후 만기의 수익률(%%), 생략 시 국고 커브 보간")
    c.add_argument("--horizon", type=float, default=1.0, help="carry: 보유 기간(년)")
    c.add_argument("--swap-points", type=float, help="hedge-cost: 스왑포인트(원, 보통 음수)")
    c.add_argument("--spot", type=float, help="hedge-cost: 현물환율")
    c.add_argument("--days", type=int, default=91, help="hedge-cost: 만기 일수")
    c.add_argument("--hedge-cost", type=float, help="hedged-yield: 헤지비용(%%p), 생략 시 전략 가정")
    c.add_argument("--exposure", type=float, help="fx-shock: 해외 익스포저(억)")
    c.add_argument("--open-pct", type=float, help="fx-shock: 환오픈 비율(%%)")
    c.add_argument("--shock", type=float, help="fx-shock: USD/KRW 변화(%%, + 원화 약세)")
    c.add_argument("--mv", type=float, help="bond-pnl: 평가액(억)")
    c.add_argument("--duration", type=float, help="bond-pnl: 수정듀레이션")
    c.add_argument("--dy-bp", type=float, help="bond-pnl: 금리 변화(bp)")
    c.add_argument("--convexity", type=float, default=0.0, help="bond-pnl: 컨벡시티")
    c.add_argument("--per", type=float, help="erp: PER(배)")
    c.add_argument("--rf", type=float, help="erp: 무위험금리(%%)")
    args = p.parse_args(argv)

    if args.cmd == "status":
        print(status_markdown(status()))
    elif args.cmd == "calc":
        print(json.dumps(_calc(args), ensure_ascii=False, indent=1))
    elif args.json:
        print(json.dumps(compute(args.cmd), ensure_ascii=False, indent=1, default=str))
    else:
        print(to_markdown(args.cmd))


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
