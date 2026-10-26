# -*- coding: utf-8 -*-
"""급등 매수 이벤트 스터디 — '공포 지표가 튀었을 때 대표 자산을 사면, 몇 개월 보유부터 승률 90% 이상인가?'

출처(일별 종가, 현지 통화):
  트리거  VIX ^VIX(yfinance — 이 계정 Refinitiv 권한 없음), MOVE ^MOVE(Refinitiv .MOVE, 2002-11~),
          VKOSPI .KSVKOSPI(Refinitiv, 시작일은 probe 결과에 따라 다름 — 코드에 박지 않는다).
  대상    나스닥100 = QQQ, KOSPI200 = KODEX 200(069500.KS), 미국 장기국채 = TLT — 모두 Refinitiv 배당 재투자 가격
          (refinitiv_src.history adjusted = TR.TotalReturn 누적, equity_daily.csv·backtest.py 와 같은 관례).
          국고 10년 총수익 = analytics 패널 ktb_10y(Refinitiv KOFIA 최종호가, %)로 재구성(아래).
          원화 환산(미국 자산 보조) = KRW=X(Refinitiv KRW=KFTC). 없으면 건너뛰고 표시.
  수집    data/history/timing_daily.csv — refinitiv_src.refresh_frame 으로 증분 갱신(YF_ONLY 는 yfinance).
          이 파일에는 계산에 쓰지 않는 ^GSPC(refinitiv_src._fill_vix_holes 가 VIX 빈 날 판정에 씀)·
          ^VIX3M(timing.py 가 이 파일에서 읽음)도 함께 유지한다(MAINTAINED).

정의(CODE):
  이벤트   종가 판정. 트리거 조건(술어)이 참이고 '무장' 상태이며 직전 이벤트에서 20거래일(트리거 지표의 거래일) 이상이면 1건,
           그 뒤 재무장 조건(술어)이 참이 될 때까지 다시 세지 않는다(히스테리시스 + 최소 20거래일 = 사용자 승인 기본값).
           무장·직전 이벤트 상태는 트리거 지표의 전체 이력으로 계산한 뒤 시작일 이후 이벤트만 남긴다 — 시작일에 이미 진행 중인
           급등 구간은 시작일에 인위적 이벤트를 만들지 않는다(연구 스크립트는 창 시작일에 '무장' 상태로 시작했다 — 여기와 다름).
           VIX ≥30(재무장 <25)·≥40(<30), VKOSPI '≥40 AND 1년 백분위 ≥90%'(<30), MOVE ≥120/140/160(<100/110/120).
           1년 백분위 = 그날을 포함한 직전 252 거래일 안 mid-rank(equity.level_stats 와 같은 정의), 252일이 차기 전엔 거짓.
           매일 센 수치(트리거가 참인 모든 날)는 비교용으로만 두고 '중복 포함·과대평가' 라벨을 붙인다.
  체결     신호일 다음 거래일의 대상 자산 종가(지표 마감과 자산 마감 시차 회피, 미래 값 사용 없음). 1회 매수가 기본.
           그 거래일이 신호일에서 14 달력일(MAX_ENTRY_DELAY — 추석·설 같은 긴 휴장은 체결)을 넘게 떨어져 있으면(대상 이력 공백) 체결 None·사유 표시.
           분할매수는 보류 — tranches=(0, 21, 42) 같은 옵션만 둔다(같은 금액씩 → 평균단가 = 조화평균, 마지막 회차 이후 보유기간만 유효, '예시').
  수익     R(h) = 가격[체결 + h] ÷ 평균단가 × (1 − 왕복비용) − 1. 승리 = R > 0. 왕복비용 기본 0.1%, 민감도 0.3%.
  보유기간 거래일 단위, 1개월 = 21거래일(연 252거래일 관례, backtest.py 의 연율화와 같은 기준):
           1M=21, 3M=63, 6M=126, 12M=252, 24M=504, 36M=756 — 대상 자산 자신의 거래일로 센다.
  표       기간마다 n·승리 수·승률·Wilson 하한·중앙값·최악값. Wilson 하한은 단측 90%(z=1.2816 = 정규분포 0.9 분위)
           (13/13 → 88.8%, 9/9 → 84.6%, 27/30 → 80.8%, 8/8 → 83.0%). 연구 메모의 '단측 90%' 수치는 z=1.645(단측 95%)로
           계산된 것이라 여기보다 낮다(13/13 → 82.8%, 9/9 → 76.9%).
  90% 도달 그 보유기간과 그보다 긴 모든 보유기간에서 승률 ≥90%, n ≥8, Wilson 하한 ≥75% 인 가장 짧은 보유기간. 없으면 '도달 안 함'.
           z=1.2816 에서는 8/8(하한 83.0%)부터 통과하므로 n ≥8 이 실제 문턱이다(7/7 은 하한 81.0% 라도 n 때문에 탈락).
  판정     verdict = 사람이 읽는 한 줄 결론, is_signal = '신호로 화면에 제시해도 되는가'(False 면 화면은 신호로 쓰지 않는다).
           본 표·최장 기간(longest)·원화 환산(krw) 표마다 붙인다.
  MOVE     12M 승률 < 70% 이거나 90% 도달 안 함이면 verdict '단독 효과 확인 안 됨'(is_signal=False).
           본 표가 '단독 효과 확인 안 됨'이면 그 스터디의 longest·krw 도 같은 판정을 단다.
  공통기간 비교 대상 전체(트리거·대상 시계열)의 원자료 첫 유효일 중 가장 늦은 날(실행 시 계산, 백분위 워밍업 없음).
           이벤트는 그날부터 센다. VKOSPI 이벤트는 1년 백분위는 252 관측이 차야 생기므로 그 전에는 생길 수 없을 뿐이다.
           지표별 최장 기간(그 트리거·대상의 원자료 첫 유효일 중 늦은 날부터)은 보조 표(longest).
  국고 10년 총수익(근사) 일간 r(%) = y_{t−1}(%) ÷ 252 − D·Δy(%p) + ½·C·Δy(%p)², D = 8, C = 0.8(%p 단위 — 소수 단위로는 80).
           만기 10년 고정(constant maturity) 가정이고 D·C 는 금리 수준과 무관하게 고정한 근사다. 관측일마다 이자 1/252.
출력: data/spikes.json — 마켓타이밍 '급등 매수' 화면(W4 연결 예정). 매매 지시가 아니라 과거 데이터 점검 자료다.

사용법: python spikes.py            # 받아서 계산·저장
        python spikes.py --offline  # 받지 않고 기존 timing_daily.csv 로만 계산
"""
import datetime
import json
import math
import os
import sys

import numpy as np
import pandas as pd

import config
import refinitiv_src as R

OUT = config.DATA_DIR / "spikes.json"
HIST = config.DATA_DIR / "history" / "timing_daily.csv"
START = "1990-01-01"
STALE_HOURS = 20
TICKERS = ["^VIX", "^MOVE", ".KSVKOSPI", "QQQ", "TLT", "069500.KS", "KRW=X"]
MAINTAINED = ["^GSPC", "^VIX3M"]   # 계산엔 안 쓰지만 같은 파일에 유지 — _fill_vix_holes(^GSPC)·timing.py(^VIX3M)
KTB = "KTB10Y"          # analytics 패널 ktb_10y(%) — load_history 가 이 키로 넣는다
KTB_TR = "KTB10TR"      # 재구성한 국고 10년 총수익 지수
H = {"1M": 21, "3M": 63, "6M": 126, "12M": 252, "24M": 504, "36M": 756}
COST, COST_SENS = 0.001, 0.003
MIN_GAP = 20
MAX_ENTRY_DELAY = 14    # 신호일 → 체결일 최대 달력일(한국 최장 휴장 10일대 + 여유, 그보다 길면 이력 공백으로 본다)
PCT_WIN = 252
KTB_D, KTB_C = 8.0, 0.8
REACH = {"win": 0.9, "n": 8, "lb": 0.75}
WILSON_Z = 1.2816      # 단측 90%(정규분포 0.9 분위)
MOVE_MIN_12M = 70.0
TRANCHES_EXAMPLE = (0, 21, 42)
NO_EFFECT = "단독 효과 확인 안 됨"
EVERY_DAY = "중복 포함·과대평가"

SOURCES = {
    "^VIX": "yfinance ^VIX (Refinitiv 권한 없음)",
    "^MOVE": "Refinitiv .MOVE",
    ".KSVKOSPI": "Refinitiv .KSVKOSPI",
    "QQQ": "Refinitiv QQQ 배당 재투자 가격(TR.TotalReturn 누적)",
    "TLT": "Refinitiv TLT 배당 재투자 가격(TR.TotalReturn 누적)",
    "069500.KS": "Refinitiv 069500.KS(KODEX 200) 배당 재투자 가격",
    "KRW=X": "Refinitiv KRW=KFTC(서울 마감 원/달러)",
    KTB: "Refinitiv KOFIA 국고 10년 최종호가(analytics 패널 ktb_10y)",
}
TARGETS = {
    "QQQ": ("나스닥100 (QQQ 총수익)", "USD"),
    "069500.KS": ("KOSPI200 (KODEX 200 총수익)", "KRW"),
    "TLT": ("미국 장기국채 (TLT 총수익)", "USD"),
    KTB_TR: ("국고 10년 총수익 (금리로 재구성)", "KRW"),
}


def pct_1y(s: pd.Series, n: int = PCT_WIN) -> pd.Series:
    """그날 포함 직전 n 거래일 안에서의 위치(0~100, mid-rank). n 일이 차기 전엔 NaN — 미래 값을 쓰지 않는다."""
    s = s.dropna()
    v = s.to_numpy(float)
    out = np.full(len(v), np.nan)
    if len(v) >= n:
        w = np.lib.stride_tricks.sliding_window_view(v, n)
        x = v[n - 1:, None]
        out[n - 1:] = ((w < x).mean(1) + 0.5 * (w == x).mean(1)) * 100
    return pd.Series(out, index=s.index)


def _ge(t):
    return lambda s: s >= t


def _lt(t):
    return lambda s: s < t


def _vkospi(s):
    return (s >= 40) & (pct_1y(s) >= 90)


# key: (이름, 지표 열, 트리거 술어, 재무장 술어, 규칙 설명, 워밍업 거래일 — 술어를 계산할 수 없는 앞부분)
TRIGGERS = {
    "vix30": ("VIX ≥ 30", "^VIX", _ge(30), _lt(25), "종가 ≥30, 25 미만에서 재무장, 직전 이벤트에서 20거래일 이상", 0),
    "vix40": ("VIX ≥ 40", "^VIX", _ge(40), _lt(30), "종가 ≥40, 30 미만에서 재무장, 직전 이벤트에서 20거래일 이상", 0),
    "vkospi40": ("VKOSPI ≥ 40 & 1년 백분위 ≥ 90%", ".KSVKOSPI", _vkospi, _lt(30),
                 "종가 ≥40 이고 1년(252거래일) 백분위 ≥90%, 30 미만에서 재무장, 직전 이벤트에서 20거래일 이상", PCT_WIN - 1),
    "move120": ("MOVE ≥ 120", "^MOVE", _ge(120), _lt(100), "종가 ≥120, 100 미만에서 재무장, 직전 이벤트에서 20거래일 이상", 0),
    "move140": ("MOVE ≥ 140", "^MOVE", _ge(140), _lt(110), "종가 ≥140, 110 미만에서 재무장, 직전 이벤트에서 20거래일 이상", 0),
    "move160": ("MOVE ≥ 160", "^MOVE", _ge(160), _lt(120), "종가 ≥160, 120 미만에서 재무장, 직전 이벤트에서 20거래일 이상", 0),
}
STUDIES = [("vix30", "QQQ"), ("vix40", "QQQ"), ("vkospi40", "069500.KS"),
           ("move120", "TLT"), ("move140", "TLT"), ("move160", "TLT"),
           ("move120", KTB_TR), ("move140", KTB_TR), ("move160", KTB_TR)]


# ── 이벤트 엔진 ──
def _scan(x: pd.Series, trigger, rearm, min_gap: int = MIN_GAP, start=None):
    """(이벤트 날짜들, 마지막 무장 상태). 술어·무장·직전 이벤트 상태는 전체 x 로 훑고, 이벤트는 start 이후만 남긴다
    (start 에 이미 진행 중인 급등 구간이 start 날짜에 인위적 이벤트를 만들지 않게)."""
    x = x.dropna()
    trig = trigger(x).reindex(x.index).fillna(False).astype(bool)
    re_ = rearm(x).reindex(x.index).fillna(False).astype(bool)
    t, r = trig.to_numpy(), re_.to_numpy()
    ev, armed, last = [], True, None
    for i, d in enumerate(x.index):
        if armed and t[i] and (last is None or i - last >= min_gap):
            ev.append(d)
            armed, last = False, i
        elif not armed and r[i]:
            armed = True
    if start is not None:
        ev = [d for d in ev if d >= pd.Timestamp(start)]
    return ev, armed


def events(x: pd.Series, trigger, rearm, min_gap: int = MIN_GAP, start=None) -> list:
    """히스테리시스 + 최소 간격 이벤트 날짜. trigger·rearm 은 시계열 → bool 시계열 술어."""
    return _scan(x, trigger, rearm, min_gap, start)[0]


def every_day(x: pd.Series, trigger, start=None) -> list:
    """트리거가 참인 모든 날(중복 포함·과대평가 — 비교용)."""
    x = x.dropna()
    t = trigger(x).reindex(x.index).fillna(False).astype(bool)
    d = x.index[t.to_numpy()]
    return list(d[d >= pd.Timestamp(start)] if start is not None else d)


# ── 보유기간 수익·승률 ──
def wilson(k: int, n: int, z: float = WILSON_Z) -> float:
    """Wilson 점수 구간 하한(비율). n=0 이면 NaN."""
    if n == 0:
        return float("nan")
    p = k / n
    den = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / den


def fwd(px: pd.Series, ev: list, cost: float = COST, tranches=None, horizons=H) -> list[dict]:
    """이벤트마다 체결일·평균단가·보유기간별 수익(비율). 체결 = 신호일 '다음' 대상 거래일 종가(미래 값 없음).
    체결일이 아직 없거나(최근 신호) 보유기간이 데이터 밖이면 None. 분할매수는 마지막 회차가 데이터 밖이면 체결 None.
    다음 대상 거래일이 신호일에서 MAX_ENTRY_DELAY 달력일을 넘으면(대상 이력 공백) 체결 None, reason 에 사유."""
    px = px.dropna()
    idx, v = px.index, px.to_numpy(float)
    out = []
    for d in ev:
        d = pd.Timestamp(d)
        pos = int(idx.searchsorted(d + pd.Timedelta(days=1)))
        e = {"signal": d, "entry": None, "cost_basis": None, "r": {h: None for h in horizons}}
        if pos < len(idx) and (idx[pos] - d).days > MAX_ENTRY_DELAY:
            e["reason"] = f"다음 대상 거래일 {idx[pos].date()} 이 신호일에서 {(idx[pos] - d).days}일 뒤(> {MAX_ENTRY_DELAY}일) — 체결 안 함"
        elif pos < len(idx):
            if tranches:
                legs = [pos + o for o in tranches]
                if legs[-1] < len(idx):
                    avgc, start = len(legs) / sum(1 / v[j] for j in legs), legs[-1]
                else:
                    avgc = None
            else:
                avgc, start = v[pos], pos
            if avgc is not None:
                e.update(entry=idx[pos], cost_basis=float(avgc))
                for h, n in horizons.items():
                    j = pos + n
                    if j < len(idx) and j >= start:
                        e["r"][h] = float(v[j] / avgc * (1 - cost) - 1)
        out.append(e)
    return out


def table(rows: list[dict], horizons=H) -> list[dict]:
    """보유기간별 n·승리·승률·Wilson 하한·중앙값·최악값(% 단위). n=0 이면 통계 None."""
    out = []
    for h, days in horizons.items():
        r = np.array([e["r"][h] for e in rows if e["r"].get(h) is not None], dtype=float)
        n, k = len(r), int((r > 0).sum())
        out.append({"h": h, "days": days, "n": n, "wins": k,
                    "win": round(k / n * 100, 1) if n else None,
                    "lb": round(wilson(k, n) * 100, 1) if n else None,
                    "median": round(float(np.median(r)) * 100, 2) if n else None,
                    "worst": round(float(r.min()) * 100, 2) if n else None})
    return out


def reach_90(tab: list[dict]) -> str | None:
    """그 보유기간과 더 긴 모든 보유기간에서 승률 ≥90%·n ≥8·Wilson 하한 ≥75% 인 가장 짧은 보유기간(없으면 None)."""
    ok = [r["n"] >= REACH["n"] and r["wins"] / r["n"] >= REACH["win"] and wilson(r["wins"], r["n"]) >= REACH["lb"]
          for r in tab]
    for i, r in enumerate(tab):
        if all(ok[i:]):
            return r["h"]
    return None


def move_verdict(tab: list[dict], reach: str | None) -> tuple[str, bool]:
    """MOVE: 12M 승률 < 70% 이거나 90% 도달 안 함 → '단독 효과 확인 안 됨'(신호 아님)."""
    r12 = next((r for r in tab if r["h"] == "12M"), None)
    if r12 is None or not r12["n"] or r12["wins"] / r12["n"] * 100 < MOVE_MIN_12M or reach is None:
        return NO_EFFECT, False
    return f"{reach}부터 승률 90% — 표본이 작아 다른 국면 확인 필요", True


def verdict(trig_key: str, tab: list[dict], reach: str | None) -> tuple[str, bool]:
    """(verdict, is_signal). verdict = 사람이 읽는 한 줄 결론, is_signal = '신호로 화면에 제시해도 되는가'.
    MOVE 는 move_verdict 규칙, 그 밖은 90% 도달 여부와 무관하게 신호로 제시(표가 도달 여부를 보여 준다)."""
    if trig_key.startswith("move"):
        return move_verdict(tab, reach)
    if reach:
        return f"{reach} 이상 보유하면 승률 90% 이상(이후 기간 모두 유지)", True
    return "승률 90% 도달 안 함", True


# ── 대상 시계열 ──
def ktb_tr(y_pct: pd.Series, dur: float = KTB_D, conv: float = KTB_C) -> pd.Series:
    """국고 10년 금리(%) → 총수익 지수(100 시작). r(%) = y_{t−1}/252 − D·Δy + ½·C·Δy² (y·Δy 는 %·%p, 만기 고정 근사)."""
    y = y_pct.dropna()
    if y.empty:
        return y
    dy = y.diff()
    r = (y.shift(1) / 252 - dur * dy + 0.5 * conv * dy ** 2) / 100
    return ((1 + r.fillna(0)).cumprod() * 100).rename(KTB_TR)


def in_krw(px: pd.Series, fx: pd.Series | None) -> pd.Series | None:
    """달러 가격 × 원/달러(직전 값 이월) — 환헤지 안 한 원화 투자자 기준."""
    if fx is None or fx.dropna().empty:
        return None
    fx = fx.dropna()
    f = fx.reindex(fx.index.union(px.index)).ffill().reindex(px.index)
    return (px * f).dropna()


def _first(x: pd.Series | None, warmup: int = 0):
    x = x.dropna() if x is not None else None
    return x.index[warmup] if x is not None and len(x) > warmup else None


def common_start(series: dict, studies=STUDIES):
    """(공통 시작일, {시계열: 첫 유효일}) — 데이터가 있는 스터디의 트리거·대상 원자료 첫 유효일 중 가장 늦은 날.
    백분위 워밍업은 기다리지 않는다(VKOSPI 이벤트는 252 관측이 차기 전엔 생길 수 없을 뿐). 워밍업보다 짧은 트리거는 제외."""
    firsts = {}
    for tk, tg in studies:
        _n, col, *_r, warm = TRIGGERS[tk]
        a, b = _first(series.get(col)), _first(series.get(tg))
        if _first(series.get(col), warm) is None or b is None:
            continue
        firsts[col], firsts[tg] = a, b
    return (max(firsts.values()) if firsts else None), firsts


# ── 계산(순수) ──
def _d(x):
    return x.date().isoformat() if x is not None else None


def _pct(x, nd=2):
    return round(x * 100, nd) if x is not None else None


def _block(x, trig, rearm, px, start, cost, tranches=None, ev=None):
    ev = events(x, trig, rearm, start=start) if ev is None else ev
    rows = fwd(px, ev, cost, tranches)
    tab = table(rows)
    lvl = x.dropna()
    return {"start": _d(start), "n_events": len(ev),
            "events": [{"signal": _d(e["signal"]), "level": round(float(lvl.get(e["signal"], np.nan)), 2),
                        "entry": _d(e["entry"]), "r": {h: _pct(v) for h, v in e["r"].items()},
                        **({"reason": e["reason"]} if e.get("reason") else {})} for e in rows],
            "table": tab, "reach_90": reach_90(tab)}


def current(x: pd.Series, trigger, rearm) -> dict:
    """지금 상태 — 오늘(마지막 종가) 트리거 참인지, 무장 여부, 마지막 이벤트와 그 뒤 지난 거래일(트리거 지표 기준)."""
    x = x.dropna()
    ev, armed = _scan(x, trigger, rearm)
    t = trigger(x).reindex(x.index).fillna(False).astype(bool)
    last = ev[-1] if ev else None
    return {"as_of": _d(x.index[-1]), "value": round(float(x.iloc[-1]), 2), "active": bool(t.iloc[-1]), "armed": armed,
            "last_event": _d(last), "days_since_last_event": int(len(x) - 1 - x.index.get_loc(last)) if last is not None else None}


def compute(series: dict, cost: float = COST, cost_sens: float = COST_SENS, tranches=None) -> dict:
    """series: {'^VIX','^MOVE','.KSVKOSPI','QQQ','TLT','069500.KS','KRW=X', KTB(%)} → spikes.json 내용(NaN 없음)."""
    s = {k: v.dropna() for k, v in series.items() if v is not None and len(v.dropna())}
    if KTB in s:
        s[KTB_TR] = ktb_tr(s[KTB])
    start, firsts = common_start(s)
    if start is None:
        raise RuntimeError("급등 매수 계산에 쓸 트리거·대상 시계열이 없습니다 — timing_daily.csv 를 먼저 받으세요(python spikes.py).")
    fx = s.get("KRW=X")
    trig_out, studies, missing = {}, [], []
    for tk, (name, col, trig, rearm, rule, warm) in TRIGGERS.items():   # 대상이 없어도 '지금 상태'는 보여 준다
        if _first(s.get(col), warm) is not None:
            trig_out[tk] = {"name": name, "series": col, "rule": rule, "source": SOURCES.get(col),
                            "current": current(s[col], trig, rearm)}
    for tk, tg in STUDIES:
        name, col, trig, rearm, rule, warm = TRIGGERS[tk]
        x, px = s.get(col), s.get(tg)
        tname, ccy = TARGETS[tg]
        if _first(x, warm) is None or _first(px) is None:
            short = f"{col} 데이터 없음" + (f"(또는 {warm + 1}거래일 미만)" if warm else "") if _first(x, warm) is None else f"{tg} 데이터 없음"
            missing.append({"trigger": tk, "target": tg, "reason": short})
            continue
        main = _block(x, trig, rearm, px, start, cost)
        ev = [pd.Timestamp(e["signal"]) for e in main["events"]]
        sens_tab = table(fwd(px, ev, cost_sens))
        raw = every_day(x, trig, start)
        raw_tab = table(fwd(px, raw, cost))
        own = max(_first(x), _first(px))
        st = {"id": f"{tk}_{tg}", "trigger": tk, "trigger_name": name, "rule": rule, "target": tg, "target_name": tname,
              "ccy": ccy, "period": "common", **main,
              "cost_sens": {"cost": cost_sens * 100, "table": sens_tab, "reach_90": reach_90(sens_tab)},
              "every_day": {"label": EVERY_DAY, "n": len(raw), "table": raw_tab, "reach_90": reach_90(raw_tab)},
              "longest": _block(x, trig, rearm, px, own, cost),
              "current": trig_out[tk]["current"]}
        st["verdict"], st["is_signal"] = verdict(tk, main["table"], main["reach_90"])
        no_eff = tk.startswith("move") and not st["is_signal"]   # MOVE 본 표가 '단독 효과 확인 안 됨'이면 보조 표도 같은 판정
        lg = st["longest"]
        lg["verdict"], lg["is_signal"] = (NO_EFFECT, False) if no_eff else verdict(tk, lg["table"], lg["reach_90"])
        if ccy == "USD":
            pk = in_krw(px, fx)
            if pk is None or _first(pk) is None:
                st["krw"] = {"skipped": True, "reason": "원/달러(KRW=X) 이력 없음 — 원화 환산 생략"}
            else:
                kb = _block(x, trig, rearm, pk, max(start, _first(pk)), cost)
                kv, ks = (NO_EFFECT, False) if no_eff else verdict(tk, kb["table"], kb["reach_90"])
                st["krw"] = {"skipped": False, "label": "원화 환산(환헤지 안 함)", "start": kb["start"], "n_events": kb["n_events"],
                             "table": kb["table"], "reach_90": kb["reach_90"], "verdict": kv, "is_signal": ks}
        if tranches:
            tt = table(fwd(px, ev, cost, tranches))
            st["tranche_example"] = {"label": "분할매수 예시(규칙 보류 중 — 참고용)", "offsets": list(tranches),
                                     "table": tt, "reach_90": reach_90(tt)}
        studies.append(st)
    used = {c for st in studies for c in (TRIGGERS[st["trigger"]][1], st["target"])}
    srcs = {}
    for k in [*TICKERS, KTB]:
        if k in s:
            srcs[k] = {"label": SOURCES[k], "first": _d(s[k].index[0]), "as_of": _d(s[k].index[-1])}
    if KTB_TR in s:
        srcs[KTB_TR] = {"label": f"{KTB} 로 재구성(D={KTB_D}, C={KTB_C})", "first": _d(s[KTB_TR].index[0]),
                        "as_of": _d(s[KTB_TR].index[-1])}
    binding = [k for k, v in firsts.items() if v == start and k in used]
    meta = {
        "sources": srcs,
        "common_period": {"start": _d(start), "binding": binding, "firsts": {k: _d(v) for k, v in firsts.items() if k in used},
                          "note": "비교 대상 전체(트리거·대상)의 원자료 첫 유효일 중 가장 늦은 날부터(백분위 워밍업 없음). "
                                  "VKOSPI 이벤트는 1년 백분위(252 관측)가 차기 전엔 생길 수 없다."},
        "costs": {"round_trip": cost * 100, "sensitivity": cost_sens * 100, "unit": "%"},
        "horizons": {h: d for h, d in H.items()},
        "tranches": list(tranches) if tranches else None,
        "definitions": {
            "event": "종가 판정 · 트리거 술어 참 & 무장 & 직전 이벤트에서 20거래일 이상 → 1건, 재무장 술어가 참이 될 때까지 다시 세지 않음. "
                     "무장·직전 이벤트 상태는 지표 전체 이력으로 계산하고 시작일 이후 이벤트만 남김(시작일에 진행 중인 구간은 이벤트 아님) — "
                     "연구 스크립트는 창 시작일에 무장 상태로 시작해 이와 다르다",
            "execution": "신호일 다음 거래일의 대상 자산 종가에 1회 매수(분할매수는 보류 — 예시 옵션만). "
                         f"그 거래일이 {MAX_ENTRY_DELAY} 달력일보다 뒤면 체결 안 함(사유 표시)",
            "return": "가격[체결 + h 거래일] ÷ 평균단가 × (1 − 왕복비용) − 1, 승리 = 수익 > 0",
            "horizons": "거래일 단위 1M=21·3M=63·6M=126·12M=252·24M=504·36M=756 — "
                        "1개월 = 21거래일(연 252거래일 관례, backtest.py 의 연율화와 같은 기준)",
            "wilson": f"Wilson 하한 z={WILSON_Z}(단측 90%). 연구 메모의 '단측 90%' 수치는 z=1.645 로 계산돼 이보다 낮다",
            "reach_90": "그 보유기간과 더 긴 모든 보유기간에서 승률 ≥90%·n ≥8·Wilson 하한 ≥75% 인 가장 짧은 보유기간, 없으면 '도달 안 함'"
                        " (z=1.2816 에선 8/8 부터 하한 ≥75% 라 n ≥8 이 실제 문턱)",
            "is_signal": "신호로 화면에 제시해도 되는가 — 본 표·longest·krw 마다 verdict 와 함께",
            "move_verdict": f"MOVE: 12M 승률 < {MOVE_MIN_12M:.0f}% 이거나 90% 도달 안 함이면 '{NO_EFFECT}'(longest·krw 도 같은 판정)",
            "every_day": f"트리거가 참인 모든 날을 센 수치 — '{EVERY_DAY}', 비교용",
            "ktb_tr": f"국고 10년 총수익 근사: r(%) = y(t−1)/252 − {KTB_D}·Δy(%p) + ½·{KTB_C}·Δy(%p)², 만기 10년 고정 가정",
            "krw": "미국 자산 원화 환산 = 달러 가격 × KRW=X(직전 값 이월), 환헤지 안 함 — 보조 표",
            "vkospi_pct": "1년 백분위 = 그날 포함 직전 252거래일 mid-rank",
        },
        "notes": ["매매 지시가 아니라 과거 데이터 점검 자료입니다. 표본(n)이 작아 통계적 확신은 제한적입니다.",
                  "VIX 는 yfinance, 그 밖(MOVE 포함)은 Refinitiv(LSEG) — 사내 공유 한정 · 외부 재배포 금지."],
    }
    return _clean({"generated_at": datetime.datetime.now().isoformat(timespec="seconds"), "meta": meta,
                   "triggers": trig_out, "studies": studies, "missing": missing})


# ── 수집·저장 ──
def load_history(refresh: bool = True) -> dict:
    """{티커: 일별 시계열} — timing_daily.csv(refresh_frame 증분 갱신, Refinitiv 미연결이면 기존 이력) + 국고 10년(analytics 패널).
    갱신은 MAINTAINED(^GSPC·^VIX3M)까지 함께 하지만 돌려주는 건 계산에 쓰는 TICKERS 와 국고 10년뿐."""
    if refresh:
        df = R.refresh_frame(HIST, TICKERS + MAINTAINED, START)
    else:
        df = pd.read_csv(HIST, index_col=0, parse_dates=True) if HIST.exists() else pd.DataFrame()
    out = {}
    for t in TICKERS:
        if t in df.columns:
            x = df[t].dropna()
            out[t] = x[x.index.day_of_week < 5]
    try:
        import analytics
        p = analytics.load_panel()
        if "ktb_10y" in p:
            out[KTB] = p["ktb_10y"].dropna()
    except Exception as e:  # noqa: BLE001 — 국고 이력이 없으면 국고 스터디만 빠진다
        print(f"  경고: 국고 10년 금리 이력 없음 — {e}")
    return out


def _clean(o):
    """JSON 안전값으로(NaN·inf → None, numpy·Timestamp → 기본형)."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (pd.Timestamp, datetime.date)):
        return o.isoformat()[:10]
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return float(o) if math.isfinite(o) else None
    return o


def build(save: bool = True, refresh: bool = True, tranches=None) -> dict:
    d = compute(load_history(refresh), tranches=tranches)
    if save:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
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


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = build(refresh="--offline" not in sys.argv,
              tranches=TRANCHES_EXAMPLE if "--split" in sys.argv else None)
    print(f"공통기간 {d['meta']['common_period']['start']}~ (기준 {', '.join(d['meta']['common_period']['binding'])})")
    for st in d["studies"]:
        r12 = next(r for r in st["table"] if r["h"] == "12M")
        print(f"{st['trigger_name']:<28} → {st['target_name']:<26} n={st['n_events']:<3} 12M {r12['win']}% "
              f"90% 도달 {st['reach_90'] or '도달 안 함'} · {st['verdict']}")
    for m in d["missing"]:
        print(f"생략: {m['trigger']} → {m['target']} ({m['reason']})")
    print(OUT)
