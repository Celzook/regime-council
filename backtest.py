# -*- coding: utf-8 -*-
"""주식 결합 전략 백테스트 — 유니버스 문서의 주식 전략을 '고르고 → 기간만 정하고 → 실행'으로 돌린다.

숫자는 모두 코드(CODE)가 계산한다. 데이터는 주식 대시보드가 받아 둔 일별 가격(data/history/equity_daily.csv)과 단기금리.
- 지수 총수익: S&P500 = SPY, KOSPI200 = KODEX 200(배당 재투자 반영), 나스닥100·닛케이·유로스톡스 = 지수 + 배당수익률 가정
- 현금(단기금리): 미국 = 13주 국채(^IRX), 한국 = 국고 3년 − 0.3%p(KOFIA, 2012년~ — CD 금리 이력이 없어 대용)
- 선물 = 지수 총수익 − 현금(완전 담보 가정) · 롤 비용 연 0.3%p(노출 비례) · 노출이 바뀔 때 거래비용 0.05%
- 옵션 = 블랙숄즈 추정 + 거래비용(건당 명목 0.02% + 프리미엄 3%). 내재변동성: S&P500 = VIX, 나스닥100 = VXN, KOSPI200 = 60일 실제 변동성 × (VIX ÷ S&P 60일 실제 변동성).
  행사가가 멀수록 변동성을 가산(지수 옵션 스큐 근사). 매월 첫 거래일에 1개월 옵션을 새로 잡는다.
결과: 연 수익률·변동성·최대 낙폭·수익/위험(샤프)·월 승률·연도별 수익·누적 곡선과 '그냥 보유' 대비 쉬운 말 결론.
매매 지시가 아니라 과거 데이터로 본 점검 자료다. 비용·스큐·대용 지표 때문에 실제와 다를 수 있다.

사용법:
  python backtest.py                 # 모든 전략을 기본값(10년)으로 돌려 data/backtest_summary.json 저장
  python backtest.py EQ-FUT-03       # 한 전략만
"""
import datetime
import json
import math
import os
import sys

import numpy as np
import pandas as pd

import config
import universe

HIST = config.DATA_DIR / "history" / "equity_daily.csv"
KOFIA = config.DATA_DIR / "history" / "kofia_daily.csv"
SUMMARY = config.DATA_DIR / "backtest_summary.json"
COST = 0.0005   # 노출 1 단위 변경당 거래비용
ROLL = 0.003    # 선물 롤 비용(연, 노출 비례)
OPT_COST = (0.0002, 0.03)   # 옵션 1건 거래비용: 명목의 0.02% + 프리미엄의 3%(호가 차이 근사)

# key: 이름, 총수익 티커, 옵션 정산 가격 티커, 가격지수일 때 더할 배당수익률, 통화, 변동성 지수, 옵션 계산용 배당수익률
UNDER = {
    "kospi200": ("KOSPI200", "069500.KS", "069500.KS", 0.0, "KRW", None, 0.018),
    "sp500": ("S&P500", "SPY", "^GSPC", 0.0, "USD", "^VIX", 0.013),
    "ndx": ("나스닥100", "^NDX", "^NDX", 0.008, "USD", "^VXN", 0.008),
    "nikkei": ("닛케이225", "^N225", "^N225", 0.018, "JPY", None, 0.018),
    "sx5e": ("유로스톡스50", "^STOXX50E", "^STOXX50E", 0.03, "EUR", None, 0.03),
}
FACTORS = {"f_minvol": ("저변동성", "USMV"), "f_quality": ("퀄리티", "QUAL"), "f_div": ("배당", "SCHD"),
           "f_value": ("가치", "VLUE"), "f_growth": ("성장", "IWF"), "f_mom": ("모멘텀", "MTUM")}
PERIODS = {"1y": ("최근 1년", 1), "3y": ("최근 3년", 3), "5y": ("최근 5년", 5), "10y": ("최근 10년", 10),
           "ytd": ("올해", "ytd"), "all": ("전체", None)}
# 과거 국면 — universe/regimes.json(사건, 날짜는 가격 데이터·기사로 확인) + 가격 데이터로 찾은 큰 급락·급등
REGIMES = config.BASE_DIR / "universe" / "regimes.json"
AUTO_CAT = "데이터로 찾은 국면"
MIN_DAYS = 30   # 계산에 필요한 최소 거래일(약 1.5개월)
SHORT_YEARS = 1.05   # 이보다 짧은 기간은 연율로 늘리지 않고 기간 수익률로 보여 준다(한 기준을 결론·화면·비교표가 같이 씀)


def _opt(*pairs):
    return [{"value": v, "label": l} for v, l in pairs]


U_MAIN = _opt(("kospi200", "KOSPI200"), ("sp500", "S&P500"), ("ndx", "나스닥100"))
U_PAIR = _opt(("sp500", "S&P500"), ("kospi200", "KOSPI200"), ("ndx", "나스닥100"), ("nikkei", "닛케이225"), ("sx5e", "유로스톡스50"))
U_OPT = _opt(("sp500", "S&P500 (VIX 사용)"), ("ndx", "나스닥100 (VXN 사용)"), ("kospi200", "KOSPI200 (변동성 근사)"))
PCT = lambda *xs: _opt(*[(x, f"지금 가격의 {x}%") for x in xs])  # noqa: E731

# 화면이 그대로 쓰는 전략 목록 — 손잡이는 1~3개, 기본값으로 바로 실행되게
CATALOG = {
    "EQ-FUT-01": {"how": "지수가 이동평균 위면 100% 사고, 아래면 줄이거나 팝니다(다음 날 반영).", "params": [
        {"key": "under", "label": "대상 지수", "options": U_MAIN, "default": "kospi200"},
        {"key": "ma", "label": "추세 기준", "options": _opt((60, "60일 이동평균 (약 3개월)"), (120, "120일 (약 6개월)"), (200, "200일 (약 10개월)")), "default": 200},
        {"key": "down", "label": "추세 아래일 때", "options": _opt(("cash", "현금으로 (0%)"), ("half", "절반 숏 (−50%)"), ("short", "전부 숏 (−100%)")), "default": "half"}]},
    "EQ-FUT-02": {"how": "두 지수 중 최근 3개월 더 강한 쪽을 사고 약한 쪽을 팝니다(매월 초 재조정). 현금 위에 얹는 초과수익으로 봅니다.", "params": [
        {"key": "a", "label": "지수 A", "options": U_PAIR, "default": "sp500"},
        {"key": "b", "label": "지수 B", "options": U_PAIR, "default": "kospi200"},
        {"key": "mode", "label": "방식", "options": _opt(("momentum", "3개월 강한 쪽 롱 / 약한 쪽 숏 (매월)"), ("fixed", "항상 A 롱 / B 숏")), "default": "momentum"}]},
    "EQ-FUT-03": {"how": "최근 60일 흔들림이 크면 노출을 줄이고, 작으면 늘려 일정한 흔들림을 유지합니다.", "params": [
        {"key": "under", "label": "대상 지수", "options": U_MAIN, "default": "kospi200"},
        {"key": "target", "label": "목표 변동성 (연)", "options": _opt((10, "10%"), (15, "15%"), (20, "20%")), "default": 15},
        {"key": "cap", "label": "최대 노출", "options": _opt((1.0, "100% (빌리지 않음)"), (1.5, "150%")), "default": 1.0}]},
    "EQ-TRS-01": {"how": "현물 대신 TRS 로 지수 수익을 받습니다. 현물 보유와 같고 조달 스프레드만큼 낮습니다.", "params": [
        {"key": "under", "label": "대상 지수", "options": U_MAIN, "default": "kospi200"},
        {"key": "spread", "label": "조달 스프레드 (연)", "options": _opt((0.3, "0.3%"), (0.5, "0.5%"), (1.0, "1.0%")), "default": 0.5}]},
    "EQ-TRS-02": {"how": "고른 팩터 ETF 를 똑같은 비중으로 담은 바스켓 TRS 를 받습니다(미국 ETF 대용). S&P500 과 비교합니다.", "params": [
        {"key": "basket", "label": "바스켓", "options": _opt(("defensive", "방어형: 저변동성·퀄리티·배당"), ("value_mom", "가치·모멘텀"), ("all", "6개 팩터 전부")), "default": "defensive"},
        {"key": "spread", "label": "조달 스프레드 (연)", "options": _opt((0.3, "0.3%"), (0.5, "0.5%"), (1.0, "1.0%")), "default": 0.5}]},
    "EQ-TRS-03": {"how": "QIS 대신 규칙을 직접 계산: 미국·한국·일본·유럽 지수를 최근 수익이 플러스면 롱, 마이너스면 숏(매월 초, 25%씩).", "params": [
        {"key": "look", "label": "추세 판단 기간", "options": _opt((6, "최근 6개월"), (12, "최근 12개월")), "default": 12},
        {"key": "fee", "label": "지수 수수료 (연)", "options": _opt((0.5, "0.5%"), (1.0, "1.0%")), "default": 0.5}]},
    "EQ-OPT-01": {"how": "지수를 들고 매월 1개월 풋을 삽니다.", "params": [
        {"key": "under", "label": "대상 지수", "options": U_OPT, "default": "sp500"},
        {"key": "put", "label": "풋 행사가", "options": PCT(90, 95, 100), "default": 95}]},
    "EQ-OPT-02": {"how": "지수를 들고 매월 풋을 사고 콜을 팔아 보험료를 줄입니다.", "params": [
        {"key": "under", "label": "대상 지수", "options": U_OPT, "default": "sp500"},
        {"key": "put", "label": "풋 행사가 (살 것)", "options": PCT(90, 95), "default": 95},
        {"key": "call", "label": "콜 행사가 (팔 것)", "options": PCT(103, 105, 110), "default": 105}]},
    "EQ-OPT-03": {"how": "지수를 들고 풋 스프레드(풋 매수 + 더 낮은 풋 매도)를 사고 콜을 팝니다.", "params": [
        {"key": "under", "label": "대상 지수", "options": U_OPT, "default": "sp500"},
        {"key": "put", "label": "풋 행사가 (살 것)", "options": PCT(95, 100), "default": 95},
        {"key": "low", "label": "더 낮은 풋 (팔 것)", "options": PCT(85, 90), "default": 85},
        {"key": "call", "label": "콜 행사가 (팔 것)", "options": PCT(105, 110), "default": 105}]},
    "EQ-OPT-04": {"how": "지수를 들고 매월 1개월 콜을 팔아 프리미엄을 받습니다.", "params": [
        {"key": "under", "label": "대상 지수", "options": U_OPT, "default": "sp500"},
        {"key": "call", "label": "콜 행사가 (팔 것)", "options": PCT(100, 102, 105), "default": 102}]},
    "EQ-OPT-05": {"how": "돈은 현금(단기금리)으로 두고 매월 지수 콜을 사서 오를 때만 참여합니다.", "params": [
        {"key": "under", "label": "대상 지수", "options": U_OPT, "default": "sp500"},
        {"key": "call", "label": "콜 행사가 (살 것)", "options": PCT(95, 100, 105), "default": 100}]},
    "EQ-OPT-06": {"how": "돈은 현금으로 두고 콜을 사면서 더 높은 콜을 팔아 비용을 줄입니다(목표까지만 참여).", "params": [
        {"key": "under", "label": "대상 지수", "options": U_OPT, "default": "sp500"},
        {"key": "call", "label": "콜 행사가 (살 것)", "options": PCT(100, 102), "default": 100},
        {"key": "high", "label": "더 높은 콜 (팔 것)", "options": PCT(105, 110, 115), "default": 110}]},
    "EQ-STR-01": {"how": "매년 초 1년 만기 원금보장형: 이자만큼으로 지수 콜을 사서 오르면 일부 참여(참여율은 그때 금리·변동성으로 계산).", "params": [
        {"key": "under", "label": "대상 지수", "options": _opt(("kospi200", "KOSPI200"), ("sp500", "S&P500")), "default": "kospi200"},
        {"key": "fee", "label": "발행 비용 (연)", "options": _opt((0.3, "0.3%"), (0.5, "0.5%"), (1.0, "1.0%")), "default": 0.5}]},
    "EQ-STR-03": {"how": "기간 초마다 하한(그때 평가액의 몇 %)을 정하고, 하한 위 여유의 몇 배만 주식에 넣습니다. 빠질수록 주식을 줄입니다. 하한은 기간 초 평가액 기준이라, 기간 중 고점에서 잰 최대 낙폭은 하한 폭보다 클 수 있습니다.", "params": [
        {"key": "under", "label": "대상 지수", "options": _opt(("kospi200", "KOSPI200"), ("sp500", "S&P500")), "default": "kospi200"},
        {"key": "floor", "label": "하한 (기간 초 평가액 대비)", "options": _opt((80, "80% (−20%)"), (85, "85% (−15%)"), (90, "90% (−10%)")), "default": 90},
        {"key": "m", "label": "승수 (여유의 몇 배)", "options": _opt((3, "3배"), (4, "4배"), (5, "5배")), "default": 4},
        {"key": "reset", "label": "하한 다시 정하기", "options": _opt(("year", "매년 초"), ("month", "매월 초 (오른 만큼 자주 잠금)")), "default": "year"}]},
}
UNAVAILABLE = {"EQ-FUT-04": "이벤트 일정(FOMC·CPI·실적 발표일) 데이터가 필요합니다.",
               "EQ-OPT-07": "VIX 선물·옵션 가격이 필요합니다(VIX 지수만으로는 계산할 수 없음).",
               "EQ-STR-02": "ELS 는 발행 조건(녹인·조기상환 일정) 가정이 많아 다음 단계로 둡니다."}


# ── 데이터 ──────────────────────────────────────────────────────
_C: dict = {}


def _px() -> pd.DataFrame:
    mt = HIST.stat().st_mtime
    if _C.get("mt") != mt:
        df = pd.read_csv(HIST, index_col=0, parse_dates=True)
        _C.update(px=df[df.index.dayofweek < 5], mt=mt)
    return _C["px"]


def _s(t: str) -> pd.Series:
    df = _px()
    if t not in df:
        raise ValueError(f"가격 데이터에 {t} 가 없습니다 — 주식 탭에서 '다시 받기'를 먼저 눌러주세요.")
    return df[t].dropna()


def tr_ret(key: str) -> pd.Series:
    """일별 총수익률(가격지수면 배당수익률 가정을 더함)."""
    _, trt, _, div, *_ = UNDER[key]
    r = _s(trt).pct_change().dropna()
    return r + div / 252 if div else r


def _align(r: pd.Series, idx: pd.DatetimeIndex) -> pd.Series:
    """벤치마크 일간 수익을 전략 날짜에 맞춘다. 전략에 없는 날의 수익은 버리지 않고 다음 전략 날짜로 복리 합산한다
    (reindex().fillna(0) 은 전략이 빠진 날 — 예: VIX 가 빈 날 — 의 벤치마크 수익을 통째로 잃었다. 2026-10-23 검증)."""
    if len(idx) == 0:
        return pd.Series(dtype=float, index=idx)
    lvl = (1 + r.dropna()).cumprod()
    L = lvl.reindex(lvl.index.union(idx)).ffill().fillna(1.0)
    Li = L.reindex(idx)
    out = Li / Li.shift(1) - 1
    before = L[L.index < idx[0]]
    out.iloc[0] = float(Li.iloc[0]) / (float(before.iloc[-1]) if len(before) else 1.0) - 1
    return out.fillna(0)


def cash(ccy: str, idx: pd.DatetimeIndex) -> pd.Series:
    """일별 현금 수익률. 한국 = 국고3년 − 0.3%p(2012~), 미국 = 13주 국채."""
    if ccy == "KRW":
        k = pd.read_csv(KOFIA, index_col=0, parse_dates=True)["ktb_3y"].dropna()
        y = (k - 0.3) / 100
    else:
        y = _s("^IRX") / 100
    return y.reindex(idx.union(y.index)).ffill().reindex(idx) / 252


def _iv(key: str) -> pd.Series:
    """옵션 계산용 연 내재변동성."""
    vix = UNDER[key][5]
    if vix:
        return _s(vix) / 100
    rv = _s(UNDER[key][2]).pct_change().rolling(60).std() * math.sqrt(252)
    spx = _s("^GSPC").pct_change().rolling(60).std() * math.sqrt(252)
    ratio = (_s("^VIX") / 100 / spx).clip(0.8, 1.6)
    return (rv * ratio.reindex(rv.index).ffill()).dropna()


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, r, q, sig, call: bool) -> float:
    if T <= 1e-6:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    sig = max(sig, 0.01)
    d1 = (math.log(S / K) + (r - q + sig * sig / 2) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if call:
        return S * math.exp(-q * T) * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * math.exp(-q * T) * _ncdf(-d1)


def _skew(iv: float, k_over_s: float) -> float:
    """지수 옵션 스큐 근사: 낮은 행사가(풋)는 변동성 가산, 높은 행사가(콜)는 감산."""
    m = k_over_s - 1
    return iv * (1 + 1.5 * -m) if m < 0 else max(iv * (1 - 0.8 * m), iv * 0.5)


def _month_starts(idx: pd.DatetimeIndex) -> list:
    s = pd.Series(idx, index=idx)
    return list(s.groupby([idx.year, idx.month]).first())


def _year_starts(idx: pd.DatetimeIndex) -> list:
    s = pd.Series(idx, index=idx)
    return list(s.groupby(idx.year).first())


# ── 전략 ────────────────────────────────────────────────────────
def s_tactical(p, start):
    k = p["under"]; r = tr_ret(k)
    nav = (1 + r).cumprod(); ma = nav.rolling(int(p["ma"])).mean()
    down = {"cash": 0.0, "half": -0.5, "short": -1.0}[p["down"]]
    E = pd.Series(np.where(nav > ma, 1.0, down), r.index).where(ma.notna()).shift(1).dropna()
    r = r.reindex(E.index); c = cash(UNDER[k][4], E.index)
    ret = E * r + (1 - E) * c - E.abs() * ROLL / 252 - E.diff().abs().fillna(0) * COST
    return ret, r, {"평균 노출": f"{E.loc[start:].mean() * 100:.0f}%", "노출 변경 횟수": f"{int((E.loc[start:].diff().abs() > 0).sum())}회"}


def _tr_index(keys) -> pd.DataFrame:
    df = pd.concat({k: (1 + tr_ret(k)).cumprod() for k in keys}, axis=1, sort=True).ffill().dropna()
    return df


def s_pair(p, start):
    a, b = p["a"], p["b"]
    if a == b:
        raise ValueError("지수 A 와 B 를 다르게 골라주세요.")
    idx = _tr_index([a, b]); r = idx.pct_change().dropna()
    spread = r[a] - r[b]
    if p["mode"] == "fixed":
        w = pd.Series(1.0, spread.index)
    else:
        mom = idx.pct_change(63)
        sig = np.sign(mom[a] - mom[b])
        ms = set(_month_starts(idx.index))
        w = sig.where([d in ms for d in idx.index]).ffill().shift(1).reindex(spread.index).dropna()
        spread = spread.reindex(w.index)
    ret = w * spread - w.diff().abs().fillna(0) * 2 * COST
    return ret, None, {"A 롱 비중": f"{(w.loc[start:] > 0).mean() * 100:.0f}%의 기간"}


def s_voltarget(p, start):
    k = p["under"]; r = tr_ret(k)
    rv = r.rolling(60).std() * math.sqrt(252)
    E = (p["target"] / 100 / rv).clip(upper=float(p["cap"])).shift(1).dropna()
    r = r.reindex(E.index); c = cash(UNDER[k][4], E.index)
    ret = E * r + (1 - E) * c - E * ROLL / 252 - E.diff().abs().fillna(0) * COST
    return ret, r, {"평균 노출": f"{E.loc[start:].mean() * 100:.0f}%", "최저 노출": f"{E.loc[start:].min() * 100:.0f}%"}


def s_index_trs(p, start):
    r = tr_ret(p["under"])
    return r - float(p["spread"]) / 100 / 252, r, {}


def s_factor_trs(p, start):
    keys = {"defensive": ["f_minvol", "f_quality", "f_div"], "value_mom": ["f_value", "f_mom"], "all": list(FACTORS)}[p["basket"]]
    df = pd.concat({k: _s(FACTORS[k][1]) for k in keys}, axis=1).dropna().pct_change().dropna()
    ret = df.mean(axis=1) - float(p["spread"]) / 100 / 252          # 매일 같은 비중(월 재조정의 근사)
    bench = _align(tr_ret("sp500"), ret.index)
    return ret, bench, {"바스켓": " · ".join(FACTORS[k][0] for k in keys), "데이터 시작": df.index[0].date().isoformat()}


def s_qis(p, start):
    keys = ["sp500", "kospi200", "nikkei", "sx5e"]
    idx = _tr_index(keys); r = idx.pct_change().dropna()
    look = {6: 126, 12: 252}[int(p["look"])]
    sig = np.sign(idx.pct_change(look)) / len(keys)
    ms = set(_month_starts(idx.index))
    W = sig.where(pd.Series([d in ms for d in idx.index], idx.index), axis=0).ffill().shift(1).reindex(r.index).dropna()
    r = r.reindex(W.index)
    c = cash("USD", W.index)
    ret = c + (W * r.sub(c, axis=0)).sum(axis=1) - float(p["fee"]) / 100 / 252 - W.diff().abs().sum(axis=1).fillna(0) * COST
    bench = _align(tr_ret("sp500"), ret.index)
    return ret, bench, {"평균 순노출": f"{W.loc[start:].sum(axis=1).mean() * 100:+.0f}%"}


def _option_run(key, legs, stock: bool, start):
    """legs: [(call?, 행사가 %, 수량 +1 매수 / −1 매도)] — 매월 첫 거래일에 1개월 옵션을 새로 잡는다."""
    S = _s(UNDER[key][2]); TR = (1 + tr_ret(key)).cumprod(); IV = _iv(key)
    idx = S.index.intersection(TR.index).intersection(IV.index)
    idx = idx[idx >= pd.Timestamp(start) - pd.Timedelta(days=40)]
    c = cash(UNDER[key][4], idx).dropna()
    idx = idx.intersection(c.index)
    S, TR, IV, c = S.reindex(idx), TR.reindex(idx), IV.reindex(idx), c.reindex(idx)
    q = UNDER[key][6]
    rolls = [d for d in _month_starts(idx) if d >= idx[0]]
    nav, out, prem_paid = 1.0, {}, []
    for i, t0 in enumerate(rolls):
        # 만기는 데이터와 무관한 규칙(다음 달 첫 영업일)으로 모든 구간에 같이 정한다 — 데이터 끝이나 실제 롤 날짜를
        # 만기로 쓰면 새 데이터가 올 때마다 지난 구간의 일간 수익이 다시 계산된다. 롤(구간 경계)은 실제 첫 거래일.
        exp = t0.normalize() + pd.offsets.BMonthBegin(1)
        t1 = rolls[i + 1] if i + 1 < len(rolls) else exp
        if t1 <= t0:
            break
        days = idx[(idx > t0) & (idx <= t1)]
        S0, r0 = float(S[t0]), float(c[t0]) * 252
        T0 = (exp - t0).days / 365
        units = nav / S0
        strikes = [(call, S0 * k / 100, n) for call, k, n in legs]
        legp = [(n, bs(S0, K, T0, r0, q, _skew(float(IV[t0]), K / S0), call)) for call, K, n in strikes]
        price0 = sum(n * v for n, v in legp)
        tcost = sum(OPT_COST[0] * S0 + OPT_COST[1] * v for _, v in legp)
        prem_paid.append((price0 + tcost) / S0)
        cash0 = (0.0 if stock else nav) - units * (price0 + tcost)
        prev = nav
        for t in days:
            tau = max((exp - t).days, 0) / 365
            St = float(S[t]); rt = float(c[t]) * 252
            opt = sum(n * bs(St, K, tau, rt, q, _skew(float(IV[t]), K / St), call) for call, K, n in strikes)
            grow = math.exp(rt * (t - t0).days / 365)
            v = (nav * float(TR[t] / TR[t0]) if stock else 0.0) + units * opt + cash0 * grow
            out[t] = v / prev - 1
            prev = v
        nav = prev
    ret = pd.Series(out).sort_index()
    bench = _align(tr_ret(key), ret.index)
    avg = np.mean(prem_paid) * 100 if prem_paid else 0
    return ret, bench, {"월평균 옵션 순비용": f"{avg:+.2f}% (+ 는 지불, − 는 수취)", "변동성": "VIX" if key == "sp500" else "VXN" if key == "ndx" else "실제 변동성 × (VIX÷S&P 실제 변동성) 근사"}


def s_opt(legs_fn, stock):
    return lambda p, start: _option_run(p["under"], legs_fn(p), stock, start)


def s_elb(p, start):
    key = p["under"]; S = _s(UNDER[key][2]); IV = _iv(key)
    idx = S.index.intersection(IV.index)
    c = cash(UNDER[key][4], idx).dropna(); idx = idx.intersection(c.index)
    idx = idx[idx >= pd.Timestamp(start) - pd.Timedelta(days=400)]
    S, IV, c = S.reindex(idx), IV.reindex(idx), c.reindex(idx)
    q, fee = UNDER[key][6], float(p["fee"]) / 100
    rolls = _year_starts(idx)
    nav, out, parts = 1.0, {}, []
    for i, t0 in enumerate(rolls):
        exp = t0.normalize() + pd.offsets.BYearBegin(1)    # 만기 = 다음 해 첫 영업일(데이터와 무관한 규칙)
        t1 = rolls[i + 1] if i + 1 < len(rolls) else exp
        if t1 <= t0:
            break
        S0, r0, T0 = float(S[t0]), float(c[t0]) * 252, (exp - t0).days / 365
        budget = 1 - math.exp(-r0 * T0) - fee * T0
        call0 = bs(S0, S0, T0, r0, q, float(IV[t0]), True) / S0
        part = max(0.0, budget / call0) if call0 > 0 else 0.0
        parts.append(part)
        prev = nav
        for t in idx[(idx > t0) & (idx <= t1)]:
            tau = max((exp - t).days, 0) / 365; rt = float(c[t]) * 252
            v_unit = math.exp(-rt * tau) + part * bs(float(S[t]), S0, tau, rt, q, float(IV[t]), True) / S0
            v0_unit = math.exp(-r0 * T0) + part * call0
            v = nav * v_unit / v0_unit
            out[t] = v / prev - 1
            prev = v
        nav = prev
    ret = pd.Series(out).sort_index()
    return ret, _align(tr_ret(key), ret.index), {"평균 참여율": f"{np.mean(parts) * 100:.0f}% (지수 상승분 중 받는 비율)" if parts else "-",
                                                           "주의": "발행사 신용위험·중도 환매 손실은 반영 안 함"}


def s_cppi(p, start):
    k = p["under"]; r = tr_ret(k)
    c = cash(UNDER[k][4], r.index).dropna(); r = r.reindex(c.index)
    r = r[r.index >= pd.Timestamp(start) - pd.Timedelta(days=5)]
    c = c.reindex(r.index)
    ys = set(_year_starts(r.index) if p["reset"] == "year" else _month_starts(r.index))
    nav, floor, E, out, expo = 1.0, None, 0.0, {}, []
    for t in r.index:
        if floor is None or t in ys:
            floor = nav * float(p["floor"]) / 100
        prev = nav
        nav *= 1 + E * float(r[t]) + (1 - E) * float(c[t])
        newE = min(1.0, max(0.0, float(p["m"]) * (nav - floor) / nav))
        nav *= 1 - abs(newE - E) * COST           # 재조정 비용도 노출·하한을 정하는 NAV 에서 뺀다
        out[t] = nav / prev - 1
        E = newE
        expo.append(E)
    ret = pd.Series(out)
    return ret, _align(tr_ret(k), ret.index), {"평균 주식 비중": f"{np.mean(expo) * 100:.0f}%", "최저 주식 비중": f"{min(expo) * 100:.0f}%"}


RUNNERS = {
    "EQ-FUT-01": s_tactical, "EQ-FUT-02": s_pair, "EQ-FUT-03": s_voltarget,
    "EQ-TRS-01": s_index_trs, "EQ-TRS-02": s_factor_trs, "EQ-TRS-03": s_qis,
    "EQ-OPT-01": s_opt(lambda p: [(False, p["put"], 1)], True),
    "EQ-OPT-02": s_opt(lambda p: [(False, p["put"], 1), (True, p["call"], -1)], True),
    "EQ-OPT-03": s_opt(lambda p: [(False, p["put"], 1), (False, p["low"], -1), (True, p["call"], -1)], True),
    "EQ-OPT-04": s_opt(lambda p: [(True, p["call"], -1)], True),
    "EQ-OPT-05": s_opt(lambda p: [(True, p["call"], 1)], False),
    "EQ-OPT-06": s_opt(lambda p: [(True, p["call"], 1), (True, p["high"], -1)], False),
    "EQ-STR-01": s_elb, "EQ-STR-03": s_cppi,
}


# ── 성과 계산·결론 ────────────────────────────────────────────────
def _resample(x, rule):
    try:
        return x.resample(rule)
    except ValueError:
        return x.resample(rule.rstrip("E"))


def stats(ret: pd.Series, bench: pd.Series | None = None, rf: pd.Series | None = None) -> dict:
    """rf(일별 현금 수익)를 주면 샤프는 초과수익(ret − rf) 기준, 없으면 원수익 기준.
    현금 금리가 없는 날(한국 국고 3년은 2012년부터)은 0 으로 채우지 않고 샤프 계산에서 뺀다."""
    ret = ret.dropna()
    ex = (ret - rf.reindex(ret.index).ffill()).dropna() if rf is not None else ret
    if len(ex) < 60:
        ex = ret
    nav = (1 + ret).cumprod()
    yrs = max((ret.index[-1] - ret.index[0]).days / 365.25, 1e-9)
    vol = float(ret.std() * math.sqrt(252))
    dd = nav / nav.cummax() - 1
    m = _resample(1 + ret, "ME").prod() - 1
    y = _resample(1 + ret, "YE").prod() - 1
    out = {"cagr": round((float(nav.iloc[-1]) ** (1 / yrs) - 1) * 100, 2), "vol": round(vol * 100, 2),
           "mdd": round(float(dd.min()) * 100, 2), "mdd_date": dd.idxmin().date().isoformat(),
           "sharpe": round(float(ex.mean() / ex.std() * math.sqrt(252)), 2) if ex.std() > 0 else None,
           "win_m": round(float((m > 0).mean()) * 100), "best_m": round(float(m.max()) * 100, 1), "worst_m": round(float(m.min()) * 100, 1),
           "total": round((float(nav.iloc[-1]) - 1) * 100, 1), "yearly": {str(d.year): round(float(v) * 100, 1) for d, v in y.items()},
           "monthly": {f"{d:%Y-%m}": round(float(v) * 100, 1) for d, v in m.items()}}
    if bench is not None:
        bm = _resample(1 + bench.reindex(ret.index).fillna(0), "ME").prod() - 1
        bad = bm[bm <= -0.03]
        if len(bad):
            out["down_months"] = {"n": int(len(bad)), "strategy": round(float(m.reindex(bad.index).mean()) * 100, 1), "bench": round(float(bad.mean()) * 100, 1)}
    return out


def verdict(st: dict, bs_: dict | None, years: float, bench_name: str) -> str:
    y = f"{years:.0f}년" if years >= 2.5 else f"{max(1, round(years * 12))}개월"
    k, per = ("total", "") if years < SHORT_YEARS else ("cagr", "연 ")
    if bs_ is None:
        return (f"{y} 동안 {per}{st[k]:+.1f}% (현금 위에 얹는 초과수익) · 최대 낙폭 {st['mdd']:.1f}%. "
                + ("꾸준히 더해 준 편입니다." if st[k] > 0 and st["win_m"] >= 50 else "더해 주지 못했습니다." if st[k] <= 0 else "벌긴 했지만 들쭉날쭉합니다."))
    a = f"{y} 동안 {per}{st[k]:+.1f}% — {bench_name} 그냥 보유는 {per}{bs_[k]:+.1f}%. 최대 낙폭은 {st['mdd']:.1f}% (그냥 보유 {bs_['mdd']:.1f}%). "
    dr, dd = st[k] - bs_[k], st["mdd"] - bs_["mdd"]
    if dr >= 0 and dd >= 0:
        j = "수익도 더 높고 덜 빠졌습니다." if st[k] >= 0 else "그냥 보유보다 덜 잃었고 덜 빠졌습니다."
    elif dr < 0 and dd >= 2:
        j = "수익은 낮은 대신 덜 빠졌습니다 — 방어용으로 의미가 있습니다."
    elif dr >= 0:
        j = "수익은 높지만 더 크게 빠졌습니다."
    else:
        j = "수익도 낮고 덜 빠지지도 않았습니다 — 이 설정은 불리했습니다."
    return a + j


def _defaults(sid: str) -> dict:
    return {x["key"]: x["default"] for x in CATALOG[sid]["params"]}


def _same(a, b) -> bool:
    """화면은 1.0 을 '1' 로 보낸다 — 숫자는 숫자로 비교."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _coerce(sid: str, params: dict) -> dict:
    """화면에서 온 값을 손잡이 목록의 값(숫자/문자)으로 맞춘다 — 목록에 없는 값은 거부."""
    out = _defaults(sid)
    for x in CATALOG[sid]["params"]:
        if x["key"] in (params or {}):
            v = params[x["key"]]
            match = [o["value"] for o in x["options"] if _same(o["value"], v)]
            if not match:
                raise ValueError(f"'{x['label']}' 값이 목록에 없습니다: {v}")
            out[x["key"]] = match[0]
    return out


def _episodes(s: pd.Series, name: str, tag: str, min_depth: float, n_dd: int, n_up: int) -> list[dict]:
    """가격으로 찾은 국면: 고점 대비 큰 하락(고점 → 바닥) n_dd 개 + 6개월 수익이 가장 컸던 구간 n_up 개."""
    s = s.dropna(); s = s[s.index.dayofweek < 5]
    idx, last = s.index, s.index[-1]
    out, dd = [], (s / s.cummax() - 1).copy()
    for _ in range(n_dd):
        trough = dd.idxmin(); depth = float(dd[trough])
        if depth > -min_depth:
            break
        before = s[:trough]
        peak = before[before >= before.max() * 0.9999].index[-1]
        after = s[trough:]; rec = after[after >= s[peak]].index
        rec_d = rec[0] if len(rec) else None
        i0, i1 = idx.get_loc(peak), idx.get_loc(trough)
        a, b = idx[max(0, i0 - 10)], idx[min(len(idx) - 1, max(i1 + 40, i0 + 50))]
        out.append({"key": f"dd_{tag}_{peak:%Y%m}", "cat": AUTO_CAT, "name": f"{name} 급락 {peak:%Y-%m} → {trough:%Y-%m} ({depth * 100:.0f}%)",
                    "start": a.date().isoformat(), "end": b.date().isoformat(), "date": trough.date().isoformat(),
                    "desc": f"고점 {peak.date()} → 바닥 {trough.date()} · 회복 {rec_d.date() if rec_d is not None else '아직'}"})
        dd[peak:(rec_d or last)] = 0.0                     # 같은 하락을 다시 고르지 않게
    fwd = (s.shift(-126) / s - 1).dropna()
    for _ in range(n_up):
        if fwd.empty:
            break
        d0 = fwd.idxmax(); g = float(fwd[d0]); i0 = idx.get_loc(d0)
        d1 = idx[min(len(idx) - 1, i0 + 126)]
        out.append({"key": f"up_{tag}_{d0:%Y%m}", "cat": AUTO_CAT, "name": f"{name} 급등 {d0:%Y-%m} → {d1:%Y-%m} (+{g * 100:.0f}%)",
                    "start": d0.date().isoformat(), "end": d1.date().isoformat(), "desc": "6개월 상승률이 가장 컸던 구간"})
        fwd[idx[max(0, i0 - 126)]:d1] = -9.0
    return out


def scenarios() -> dict[str, dict]:
    """과거 국면 전체(키 → 항목). 가격 파일이 바뀔 때만 다시 계산."""
    key = (HIST.stat().st_mtime, REGIMES.stat().st_mtime)
    if _C.get("sc_key") != key:
        items = json.loads(REGIMES.read_text(encoding="utf-8"))["items"]
        auto = _episodes(_s("^KS11"), "KOSPI", "kospi", 0.20, 6, 3) + _episodes(_s("^GSPC"), "S&P500", "spx", 0.15, 5, 2)
        _C.update(sc={x["key"]: x for x in items + auto}, sc_key=key)
    return _C["sc"]


def _preset_dates(key: str) -> tuple[pd.Timestamp, pd.Timestamp, str]:
    last = _px().index[-1]
    sc = scenarios()
    if key in sc:
        return pd.Timestamp(sc[key]["start"]), min(pd.Timestamp(sc[key]["end"]), last), sc[key]["name"]
    if key not in PERIODS:
        raise ValueError(f"기간 값이 올바르지 않습니다: {key}")
    label, yrs = PERIODS[key]
    if yrs == "ytd":
        return pd.Timestamp(last.year, 1, 1), last, label
    return (last - pd.DateOffset(years=yrs) if yrs else _px().index[0]), last, label


def resolve_period(period: str = "10y", start: str | None = None, end: str | None = None):
    """(시작 기준일, 종료일, 키, 이름). 날짜를 직접 주면 그 구간 — 버튼 기간과 날짜가 같으면 버튼 이름을 쓴다."""
    if not (start or end):
        s, e, label = _preset_dates(period)
        return s, e, period, label
    try:
        s = pd.Timestamp(start) if start else _px().index[0]
        e = min(pd.Timestamp(end), _px().index[-1]) if end else _px().index[-1]
    except (ValueError, TypeError):
        raise ValueError("날짜 형식이 올바르지 않습니다 (예: 2020-01-01).")
    if s >= e:
        raise ValueError("시작일이 종료일보다 앞이어야 합니다.")
    if period in PERIODS or period in scenarios():
        ps, pe, label = _preset_dates(period)
        if ps.date() == s.date() and pe.date() == e.date():
            return s, e, period, label
    return s, e, "custom", f"{s.date()} ~ {e.date()}"


def run(sid: str, params: dict | None = None, period: str = "10y", start: str | None = None, end: str | None = None) -> dict:
    if sid in UNAVAILABLE:
        raise ValueError(UNAVAILABLE[sid])
    if sid not in RUNNERS:
        raise ValueError(f"알 수 없는 전략: {sid}")
    p = _coerce(sid, params)
    want, e, pkey, plabel = resolve_period(period, start, end)
    ret, bench, extra = RUNNERS[sid](p, want)
    ret = ret.dropna()
    first = ret.index[0].date() if len(ret) else None
    ret = ret[(ret.index > want) & (ret.index <= e)]
    if len(ret) < MIN_DAYS:
        raise ValueError(f"이 기간에는 계산할 수 있는 날이 {len(ret)}일뿐입니다(최소 {MIN_DAYS}일). "
                         + (f"이 전략은 {first}부터 계산할 수 있습니다" if first else "데이터가 없습니다")
                         + " — 한국 지수 전략은 현금 금리 데이터가 2012년부터라 그 전은 안 됩니다.")
    bench = _align(bench, ret.index) if bench is not None else None
    # 샤프는 현금 초과수익 기준 — 지역 상대 뷰(EQ-FUT-02)는 이미 초과수익(롱−숏)이라 현금을 빼지 않는다
    rf_key = p.get("under") or ("sp500" if sid in ("EQ-TRS-02", "EQ-TRS-03") else None)
    rf = cash(UNDER[rf_key][4], ret.index) if rf_key else None
    st = stats(ret, bench, rf)
    bst = stats(bench, rf=rf) if bench is not None else None
    years = (ret.index[-1] - ret.index[0]).days / 365.25
    u = next(s for s in universe.load()["strategies"] if s["id"] == sid)
    under_key = p.get("under") or ("sp500" if sid in ("EQ-TRS-02", "EQ-TRS-03") else None)
    bench_name = UNDER[under_key][0] if under_key else "-"
    nav = (1 + ret).cumprod(); bnav = (1 + bench).cumprod() if bench is not None else None
    daily = years < 1.5                                   # 짧은 구간은 일 단위로 그린다
    wk = nav if daily else _resample(nav, "W-FRI").last().dropna()
    dd = (nav / nav.cummax() - 1)
    labels = {x["key"]: next(o["label"] for o in x["options"] if o["value"] == p[x["key"]]) for x in CATALOG[sid]["params"]}
    return {
        "id": sid, "name": u["name"], "type": u["type"], "structure": u["structure"], "how": CATALOG[sid]["how"],
        "params": p, "param_labels": [{"label": x["label"], "value": labels[x["key"]]} for x in CATALOG[sid]["params"]],
        "period": {"key": pkey, "label": plabel, "start": ret.index[0].date().isoformat(), "end": ret.index[-1].date().isoformat(),
                   "years": round(years, 2), "per_period": years < SHORT_YEARS, "requested": [want.date().isoformat(), e.date().isoformat()],
                   "short": bool((ret.index[0] - want).days > 20) and pkey != "all",
                   "event": ({"date": scenarios()[pkey]["date"], "name": scenarios()[pkey]["name"]}
                             if scenarios().get(pkey, {}).get("date") else None)},
        "bench_name": bench_name if bench is not None else None,
        "stats": st, "bench": bst, "verdict": verdict(st, bst, years, bench_name), "extra": extra,
        "curve": {"dates": [d.date().isoformat() for d in wk.index], "nav": [round(float(v) * 100, 2) for v in wk.values],
                  "bench": [round(float(v) * 100, 2) for v in (bnav.reindex(wk.index) if daily else _resample(bnav, "W-FRI").last().reindex(wk.index)).ffill().values] if bnav is not None else None,
                  "dd": [round(float(v) * 100, 2) for v in (dd if daily else _resample(dd, "W-FRI").min().reindex(wk.index)).values]},
        "assumptions": ["과거 데이터로 본 점검 자료 — 매매 지시 아님", "선물 = 지수 총수익 − 현금(완전 담보), 롤 비용 연 0.3%p, 노출 변경 거래비용 0.05%",
                        "현금: 미국 13주 국채, 한국 국고 3년 − 0.3%p(2012년~, CD 금리 대용)",
                        "옵션: 블랙숄즈 추정·스큐 근사 — 실제 호가와 다를 수 있음",
                        "샤프: 현금(위 금리) 초과수익 ÷ 변동성 — 지역 상대 뷰는 그 자체가 초과수익. 한국 현금 금리가 없는 2012년 이전은 샤프 계산에서만 뺀다"],
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def catalog() -> dict:
    """화면용: 주식 전략 17개 + 손잡이·가능 여부."""
    eqv = universe.equity_view()
    rows = []
    for s in eqv["strategies"]:
        c = CATALOG.get(s["id"])
        rows.append({"id": s["id"], "name": s["name"], "type": s["type"], "structure": s["structure"], "scenario": s["scenario"],
                     "payoff": s["payoff"], "risk": s["risk"], "ready": s["ready"],
                     "available": bool(c), "why_not": UNAVAILABLE.get(s["id"]),
                     "how": c["how"] if c else None, "params": c["params"] if c else []})
    pre = lambda keys: [{"value": k, "label": lab, "start": s.date().isoformat(), "end": e.date().isoformat()}  # noqa: E731
                        for k in keys for s, e, lab in [_preset_dates(k)]]
    cats = json.loads(REGIMES.read_text(encoding="utf-8"))["categories"] + [AUTO_CAT]
    sc = sorted(scenarios().values(), key=lambda x: (cats.index(x["cat"]) if x["cat"] in cats else 99, -int(x["start"].replace("-", ""))))  # 분류 순, 안에서는 최근 먼저
    scen = [{"value": x["key"], "label": x["name"], "cat": x["cat"], "start": x["start"], "end": x["end"],
             "date": x.get("date"), "desc": x.get("desc", ""), "source": x.get("source")} for x in sc]
    return {"strategies": rows, "presets": pre(PERIODS), "scenarios": scen, "categories": cats, "default_period": "10y",
            "range": {"min": _px().index[0].date().isoformat(), "max": _px().index[-1].date().isoformat()},
            "notes": "한국 지수 전략은 현금 금리 데이터가 2012년부터, 팩터 ETF 는 2013년 무렵부터라 그 전 기간은 짧아질 수 있습니다."}


def run_all(period: str = "10y", start: str | None = None, end: str | None = None, save: bool = True) -> dict:
    want, e, pkey, plabel = resolve_period(period, start, end)   # 잘못된 기간이면 여기서 바로 알림
    rows = []
    for sid in RUNNERS:
        try:
            r = run(sid, None, period, start, end)
            rows.append({"id": sid, "name": r["name"], "type": r["type"], "params": r["param_labels"], "period": r["period"],
                         "total": r["stats"]["total"], "bench_total": (r["bench"] or {}).get("total"),
                         "cagr": r["stats"]["cagr"], "vol": r["stats"]["vol"], "mdd": r["stats"]["mdd"], "sharpe": r["stats"]["sharpe"],
                         "win_m": r["stats"]["win_m"], "bench_name": r["bench_name"],
                         "bench_cagr": (r["bench"] or {}).get("cagr"), "bench_mdd": (r["bench"] or {}).get("mdd"),
                         "down": r["stats"].get("down_months"), "verdict": r["verdict"]})
        except Exception as e:  # noqa: BLE001 — 한 전략 실패가 전체를 막지 않게
            rows.append({"id": sid, "error": str(e)})
    d = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"), "period": pkey, "period_label": plabel,
         "start": want.date().isoformat(), "end": e.date().isoformat(), "short_period": (e - want).days / 365.25 < SHORT_YEARS, "rows": rows,
         "note": "각 전략 기본값(화면의 첫 설정)으로 돌린 결과. 같은 기간이라도 데이터 시작이 늦은 전략은 짧을 수 있다."}
    if save:
        tmp = SUMMARY.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, SUMMARY)
    return d


def load_summary() -> dict | None:
    try:
        return json.loads(SUMMARY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        r = run(sys.argv[1], None, sys.argv[2] if len(sys.argv) > 2 else "10y")
        print(r["verdict"]); print(json.dumps({k: r["stats"][k] for k in ("cagr", "vol", "mdd", "sharpe", "win_m")}, ensure_ascii=False), r["extra"])
    else:
        d = run_all()
        for x in d["rows"]:
            print(x["id"], x.get("name"), x.get("error") or f"연 {x['cagr']}% · 변동성 {x['vol']}% · 낙폭 {x['mdd']}% · 샤프 {x['sharpe']} | 보유 {x['bench_cagr']}% / {x['bench_mdd']}%")
