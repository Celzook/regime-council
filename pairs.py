# -*- coding: utf-8 -*-
"""페어트레이딩 — 마켓타이밍 '페어' 칸. 같은 회사의 보통·우선, 상장 모·자회사, 같은 업종 대형주 짝 가운데
'월말마다 공적분이 확인된 짝'만 골라 스프레드가 벌어지면 되돌림에 거는 규칙을 과거에 돌려 본다.

문턱·비용·규제 구간은 universe/pairs.json(카탈로그)에 있다 — 숫자를 바꿀 때는 그 파일만 고친다.
입력(네트워크는 build() 의 pair_universe 호출만 — compute() 는 순수 함수):
  master   종목 마스터(index = ric): code·name·kind·market·gics_code·mktcap·org_id·idx_proxy.
  cands    후보 [{id, a, b, kind, pct, as_of, note}] — kind '보통·우선'·'모자회사'·'동종업종'(pair_universe.candidates).
  ret      날짜 × ric TR.TotalReturn 일간 수익률(%, 배당 재투자). tv = 날짜 × ric 거래대금(원).
정의(CODE):
  로그 가격  cumsum(log(1 + r/100)) — 상장 전·거래정지일은 NaN(결측일은 그 날짜만 빠진다).
  창        데이터 인덱스(전 종목 거래일 합집합)의 252 행. 그 안에서 두 종목 모두 값이 있는 날만 쓰고 min_obs(240) 개 미만이면 시험·z 안 함.
  월말 시험  달력 월의 마지막 거래일(인덱스 기준 — 다음 행이 다른 달). 마지막 행은 그날이 그 달 마지막 평일일 때만 월말로 본다.
            동종업종은 같은 창의 일간 로그수익률 상관 ≥ corr_min 일 때만 시험. EG = statsmodels coint(trend='c', AIC 자동 시차)와
            같은 계산을 빠르게 다시 구현한 eg()(동등성은 테스트가 statsmodels 와 비교) — 양방향, p = max(p_ab, p_ba).
            β = OLS(log a ~ 1 + log b). 같은 날 시험한 모든 후보의 p 로 BH-FDR q. 채택 = p_ab·p_ba < p_max, q ≤ fdr_q, 반감기 hl_min~hl_max.
            β ≤ 0 은 채택하지 않는다 — 헤지가 두 종목 같은 방향(둘 다 매수·매도)이 되어 숏 레그가 없는 '페어'가 아니고,
            비중·손익 식(w_b = |β|/(1+|β|), b 는 a 반대 방향)이 β > 0 을 전제한다(2026-10-28 실측: 진에어/대한항공 β −0.68).
            채택은 그 시험일 종가 신호부터 다음 월말 시험일 전날까지 유효.
  반감기     Δs_t = c + φ·s_{t−1}(s = a~b 잔차) → −ln2 / ln(1 + φ). φ ≥ 0 → inf, φ ≤ −1 → 정의 안 됨(None, 채택 안 함).
  z         고정 β 의 s = log a − β·log b, (s_t − 창 평균) ÷ 창 표준편차(ddof 1), 창 = t 포함 직전 252 행. 대기 중 β = 가장 최근 채택 시험의 β,
            보유 중 β = 진입 때 β(재검정으로 β 가 바뀌어도 그대로).
  상태기계   페어마다 '공동 거래일'(두 종목 모두 값 있는 날) 순서로 돈다. t 종가 신호 → 다음 공동 거래일 종가 체결, 수익은 그다음 날부터.
            진입: 채택 유효 · 쿨다운 아님 · entry_z < |z| < stop_z · 체결일 공매도 허용(banned 불가, partial 은 숏 레그 idx_proxy 만) ·
                  20일 거래대금(작은 쪽) ≥ min_tv_krw. z > 0 → a 매도·b 매수, z < 0 → a 매수·b 매도. 비중 w_a = 1/(1+|β|), w_b = |β|/(1+|β|).
            청산(진입 방향 부호로): 진입 쪽으로 stop_z 넘게 벌어지면 손절(체결일부터 ceil(반감기) 거래일 진입 금지) >
                  진입 쪽 exit_z 아래(평균을 지나쳐 반대편으로 간 날 포함)면 정상 > 보유일 ≥ min(2×반감기, 60) 시간 손절.
                  체결일 종가에도 청산 조건을 본다. 재검정 탈락·공매도 금지 시작으로는 청산하지 않는다. 데이터 끝에 열린 포지션은 미실현.
  수익       일간(%) = 방향 × (w_a·r_a − w_b·r_b), 진입 체결 다음 날 ~ 청산 체결일 합(복리 아님). net = gross − 체결·슬리피지(진입·청산 각 다리)
            − 매도분 거래세(진입 숏 다리·청산 롱 다리, 체결일 세율) − 대차료(숏 비중 × 연율 ÷ 252 × 보유일). 비용 민감도는 체결·슬리피지만 바꾼다.
출력: data/pairs.json(현재 표·백테스트 표) · data/pairs_charts.json(페어별 최근 3년 z·스프레드·거래). 매매 지시가 아니라 과거 데이터 점검 자료다.

사용법: python pairs.py            # 받아서 계산·저장
        python pairs.py --offline  # 받지 않고 저장된 가격 이력으로만 계산
"""
import datetime
import json
import math
import os
import sys

import numpy as np
import pandas as pd
from statsmodels.tsa.adfvalues import mackinnonp

import config
import universe

OUT = config.DATA_DIR / "pairs.json"
CHARTS = config.DATA_DIR / "pairs_charts.json"
CATALOG = universe.DIR / "pairs.json"
STALE_HOURS = 20
SQRTEPS = math.sqrt(np.finfo(float).eps)   # statsmodels coint 의 공선성 판정과 같은 값
LONG_A, SHORT_A = 1, -1                    # 방향: +1 = a 매수·b 매도, −1 = a 매도·b 매수
SIDE = {LONG_A: "롱 a·숏 b", SHORT_A: "숏 a·롱 b"}
STOP, NORMAL, TIME = "손절", "정상", "시간 손절"
REASONS = (NORMAL, STOP, TIME)
ST_SIGNAL, ST_HOLD, ST_WAIT, ST_COOL, ST_HOLD_OUT, ST_OUT = "진입 신호", "보유 중", "대기", "쿨다운", "미채택 — 보유 중", "미채택"
ST_WATCH = "관찰"            # 양방향 p·반감기는 통과, FDR 에서 탈락 — 신호 아님(2026-10-28 사용자 결정: 기준 유지 + 관찰 목록)
STATE_ORDER = (ST_SIGNAL, ST_HOLD, ST_HOLD_OUT, ST_COOL, ST_WAIT, ST_WATCH, ST_OUT)
REQUIRED = ("history_start", "test_window", "z_window", "min_obs", "p_max", "fdr_q", "hl_min", "hl_max", "corr_min",
            "entry_z", "exit_z", "stop_z", "time_stop_max", "time_stop_hl_mult", "cooldown_hl_mult", "min_tv_krw",
            "tv_window", "tv_min_periods", "commission_slip_per_side", "commission_slip_sens", "borrow_fee_annual",
            "days_per_year", "beta_drift_window", "beta_drift_max", "recent_months", "chart_years", "pct_bins", "verdict_min_trades")


# ── 카탈로그 ──
def load_catalog(path=CATALOG) -> dict:
    cat = json.loads(path.read_text(encoding="utf-8"))
    p = cat.get("params") or {}
    errs = [f"params.{k} 없음" for k in REQUIRED if k not in p]
    if not ((cat.get("sell_tax") or {}).get("table")):
        errs.append("sell_tax.table 없음")
    if "table" not in (cat.get("short_regimes") or {}):
        errs.append("short_regimes.table 없음")
    if not (p.get("exit_z", 0) < p.get("entry_z", 0) < p.get("stop_z", 0)):
        errs.append("exit_z < entry_z < stop_z 여야 함")
    if errs:
        raise ValueError("universe/pairs.json 오류 — " + "; ".join(errs))
    return cat


def _ts(x):
    return None if x is None else pd.Timestamp(x)


def tax_rate(cat: dict, d) -> float:
    """체결일 d 의 매도 거래세(%) — 카탈로그 표에서 d 가 속한 구간(양끝 포함)."""
    d = pd.Timestamp(d)
    for r in cat["sell_tax"]["table"]:
        if (r["from"] is None or d >= _ts(r["from"])) and (r["to"] is None or d <= _ts(r["to"])):
            return float(r["rate"])
    raise ValueError(f"거래세 구간 없음: {d.date()}")


def regime(cat: dict, d) -> str:
    """날짜 d 의 공매도 규제 구간 — 'open'|'partial'|'banned'(표에 없으면 open)."""
    d = pd.Timestamp(d)
    for r in cat["short_regimes"]["table"]:
        if _ts(r["from"]) <= d <= _ts(r["to"]):
            return r["regime"]
    return "open"


def short_allowed(rg: str, short_is_idx: bool) -> bool:
    return rg == "open" or (rg == "partial" and bool(short_is_idx))


# ── 통계(네트워크 없음) ──
def bh_fdr(p) -> np.ndarray:
    """Benjamini-Hochberg q 값(뒤에서부터 누적 최소, 1 상한)."""
    p = np.asarray(p, float)
    m = len(p)
    if not m:
        return p
    o = np.argsort(p, kind="stable")
    q = p[o] * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(m)
    out[o] = np.minimum(q, 1.0)
    return out


def _ols(y: np.ndarray, x: np.ndarray):
    """y ~ 1 + x → (기울기, 절편, 잔차)."""
    xm, ym = x.mean(), y.mean()
    dx = x - xm
    b = (dx @ (y - ym)) / (dx @ dx)
    return b, ym - b * xm, y - ym - b * dx


def half_life(s: np.ndarray) -> float | None:
    """Δs_t = c + φ·s_{t−1} → −ln2 / ln(1+φ). φ ≥ 0 → inf, φ ≤ −1 → None."""
    s = np.asarray(s, float)
    if len(s) < 3:
        return None
    phi = _ols(np.diff(s), s[:-1])[0]
    if not math.isfinite(phi) or phi <= -1:
        return None
    if phi >= 0:
        return math.inf
    return -math.log(2) / math.log1p(phi)


def _adf_t(e: np.ndarray) -> float:
    """statsmodels adfuller(e, regression='n', autolag='AIC') 의 t 값과 같은 계산.
    시차 고르기는 최대 시차로 자른 표본에서 AIC 최소(동률이면 짧은 쪽), t 는 고른 시차로 다시 자른 표본의 재적합에서."""
    n = len(e)
    kmax = min(n // 2 - 1, int(math.ceil(12.0 * (n / 100.0) ** 0.25)))
    d = np.diff(e)

    def design(k):
        m = n - 1 - k
        X = np.empty((m, k + 1))
        X[:, 0] = e[k:n - 1]
        for j in range(1, k + 1):
            X[:, j] = d[k - j:n - 1 - j]
        return X, d[k:]

    X, y = design(kmax)
    Q, R = np.linalg.qr(X)
    qy = Q.T @ y
    r = y - Q @ qy
    tail = np.cumsum((qy ** 2)[::-1])[::-1]
    ssr = r @ r + np.append(tail[1:], 0.0)            # 앞 k+1 열 회귀의 잔차제곱합(k = 0..kmax)
    aic = len(y) * np.log(ssr) + 2 * np.arange(1, kmax + 2)
    k = int(np.argmin(aic))
    X, y = design(k)
    Q, R = np.linalg.qr(X)
    coef = np.linalg.solve(R, Q.T @ y)
    r = y - X @ coef
    Ri = np.linalg.inv(R)
    return float(coef[0] / math.sqrt((r @ r) / (len(y) - (k + 1)) * (Ri[0] @ Ri[0])))


def eg(y: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    """Engle-Granger(statsmodels coint(y, x, trend='c') 와 같은 값) → (t, p)."""
    _, _, e = _ols(y, x)
    sst = ((y - y.mean()) ** 2).sum()
    if not (sst > 0 and 1 - (e @ e) / sst < 1 - 100 * SQRTEPS):
        return -math.inf, 0.0                           # 거의 완전 공선 — coint 와 같이 p = 0
    t = _adf_t(e)
    return t, float(mackinnonp(t, regression="c", N=2))


def test_pair(la: np.ndarray, lb: np.ndarray) -> dict:
    """공동 관측 로그 가격 두 줄 → {p_ab, p_ba, p, beta, hl}."""
    beta, _, e = _ols(la, lb)
    p_ab = eg(la, lb)[1]
    p_ba = eg(lb, la)[1]
    return {"p_ab": p_ab, "p_ba": p_ba, "p": max(p_ab, p_ba), "beta": float(beta), "hl": half_life(e)}


# ── 창 합(누적합 차) ──
def _csum(v: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(v)])


def _win(C: np.ndarray, pos, w: int):
    """끝 위치 pos(포함)에서 w 행 창의 합."""
    pos = np.asarray(pos)
    return C[pos + 1] - C[np.maximum(pos + 1 - w, 0)]


class Pair:
    """페어 하나의 배열(데이터 인덱스 길이) — 창 합으로 시험·z 를 O(1) 에 꺼낸다."""

    def __init__(self, la, lb, ra, rb):
        self.la, self.lb = la, lb
        m = ~np.isnan(la) & ~np.isnan(lb)
        self.m = m
        xa, xb = np.where(m, la, 0.0), np.where(m, lb, 0.0)
        self.C = {k: _csum(v) for k, v in (("n", m.astype(float)), ("a", xa), ("b", xb), ("aa", xa * xa),
                                           ("bb", xb * xb), ("ab", xa * xb))}
        mr = ~np.isnan(ra) & ~np.isnan(rb)
        ya, yb = np.where(mr, ra, 0.0), np.where(mr, rb, 0.0)
        self.R = {k: _csum(v) for k, v in (("n", mr.astype(float)), ("a", ya), ("b", yb), ("aa", ya * ya),
                                           ("bb", yb * yb), ("ab", ya * yb))}

    def count(self, pos, w):
        return _win(self.C["n"], pos, w)

    def corr(self, pos, w):
        S = {k: _win(v, pos, w) for k, v in self.R.items()}
        n = S["n"]
        with np.errstate(invalid="ignore", divide="ignore"):
            cab = S["ab"] - S["a"] * S["b"] / n
            va = S["aa"] - S["a"] ** 2 / n
            vb = S["bb"] - S["b"] ** 2 / n
            return cab / np.sqrt(va * vb)

    def moments(self, pos, w):
        """창 평균·표본분산·공분산(ddof 1) — (n, ma, mb, va, vb, cab)."""
        S = {k: _win(v, pos, w) for k, v in self.C.items()}
        n = S["n"]
        with np.errstate(invalid="ignore", divide="ignore"):
            ma, mb = S["a"] / n, S["b"] / n
            va = (S["aa"] - n * ma * ma) / (n - 1)
            vb = (S["bb"] - n * mb * mb) / (n - 1)
            cab = (S["ab"] - n * ma * mb) / (n - 1)
        return n, ma, mb, va, vb, cab


def last_session(d) -> pd.Timestamp:
    """d 가 속한 달의 마지막 예정 거래일 — 마지막 평일, 12월은 31일(KRX 연말 휴장)을 뺀 마지막 평일. 다른 공휴일은 모른다."""
    e = pd.offsets.BMonthEnd().rollforward(pd.Timestamp(d).normalize())
    if e.month == 12 and e.day == 31:
        e -= pd.offsets.BDay(1)
    return e


def month_ends(idx: pd.DatetimeIndex) -> list[int]:
    """달력 월의 마지막 거래일 위치(다음 행이 다른 달). 마지막 행은 그 달 마지막 예정 거래일(last_session) 이후일 때만."""
    if not len(idx):
        return []
    mo = idx.year * 12 + idx.month
    out = list(np.flatnonzero(mo[:-1] != mo[1:]))
    last = idx[-1]
    if last >= last_session(last):
        out.append(len(idx) - 1)
    return out


def run_tests(pairs: dict, kinds: dict, mes: list[int], P: dict, errors: list | None = None) -> dict:
    """월말 시험 — {id: [시험 결과 dict | None]}(mes 순서). None = 공동 관측 부족(시험 안 함).
    pairs 값은 Pair 또는 Pair 를 만드는 함수(후보 수천 개의 창 합을 한꺼번에 들고 있지 않게 — 시험 뒤 버린다).
    동종업종 상관 미달은 {tested: False, corr}. 시험한 결과에는 q·adopted 가 붙는다(날짜마다 BH-FDR)."""
    W, mo = int(P["test_window"]), int(P["min_obs"])
    pos = np.asarray(mes, int)
    out = {}
    for pid, pr in pairs.items():
        pr = pr() if callable(pr) else pr
        res = [None] * len(mes)
        if len(pos):
            n = pr.count(pos, W)
            cr = pr.corr(pos, W) if kinds[pid] == "동종업종" else None
            for k in np.flatnonzero(n >= mo):
                i = int(pos[k])
                r = {"n": int(n[k]), "corr": None if cr is None else float(cr[k])}
                if cr is not None and not (cr[k] >= P["corr_min"]):
                    r["tested"] = False
                    res[k] = r
                    continue
                w = slice(i - W + 1, i + 1)
                mm = pr.m[w]
                try:
                    r.update(test_pair(pr.la[w][mm], pr.lb[w][mm]), tested=True)
                except (np.linalg.LinAlgError, ValueError, ZeroDivisionError, FloatingPointError) as e:
                    if errors is not None:
                        errors.append(f"{pid} 시험 오류({k}): {e}")
                    r["tested"] = False
                res[k] = r
        out[pid] = res
    for k in range(len(mes)):                 # 같은 날 시험한 모든 후보로 BH-FDR
        ids = [pid for pid, rs in out.items() if rs[k] and rs[k].get("tested")]
        q = bh_fdr([out[pid][k]["p"] for pid in ids])
        for pid, qq in zip(ids, q):
            r = out[pid][k]
            hl = r["hl"]
            r["q"] = float(qq)
            r["adopted"] = bool(r["p_ab"] < P["p_max"] and r["p_ba"] < P["p_max"] and qq <= P["fdr_q"]
                                and hl is not None and P["hl_min"] <= hl <= P["hl_max"] and r["beta"] > 0)
    return out


# ── 상태기계 ──
def time_stop_days(hl: float, P: dict) -> float:
    return min(P["time_stop_hl_mult"] * hl, P["time_stop_max"])


def machine(n: int, z_of, adopt, wait_beta, short_ok, tv_ok, P: dict, start: int = 0):
    """공동 거래일 0..n−1 을 돈다.
    z_of(j, β) → z | None, adopt(j) → (β, 반감기) | None(그날 채택 유효 아님), wait_beta(j) → 대기 중 β | None,
    short_ok(j_fill, side) → 체결일(j_fill == n 이면 데이터 다음 날)에 그 방향의 숏 레그 허용?, tv_ok(j) → 거래대금 문턱 통과?
    반환 (trades, beta_used, end) — trade = {side, beta, hl, sig, ent, xsig, ext, reason}(j 위치, 열린 거래는 ext None),
    end = {signal: (side, β, 반감기) | None(데이터 끝 진입 신호), cool: 진입 금지 마지막 j, xsig: 데이터 끝 청산 신호 사유}."""
    ez, xz, sz = P["entry_z"], P["exit_z"], P["stop_z"]
    trades, bu = [], [None] * n
    pos, pend, cool = None, None, -1
    end = {"signal": None, "cool": -1, "xsig": None}
    for j in range(start, n):
        if pend is not None:
            if pend[0] == "in":
                pos = {"side": pend[1], "beta": pend[2], "hl": pend[3], "sig": pend[4], "ent": j,
                       "xsig": None, "ext": None, "reason": None}
                pend = None
            else:
                pos.update(reason=pend[1], xsig=pend[2], ext=j)
                trades.append(pos)
                bu[j] = pos["beta"]
                if pend[1] == STOP:
                    cool = j + math.ceil(P["cooldown_hl_mult"] * pos["hl"])
                pos, pend = None, None
                continue                               # 청산 체결일엔 새 신호 없음
        if pos is not None:
            b = pos["beta"]
            bu[j] = b
            z = z_of(j, b)
            why = None
            # 부호로 본다 — 숏 a(z>+2 진입)는 z 가 +stop 위면 손절, +exit 아래(평균 너머 음수 포함)면 정상. 롱 a 는 반대.
            # |z| 로 보면 평균을 지나쳐 튄 날 청산하지 못하고, 반대편 −3 이 이익 중인 거래를 손절로 친다(2026-10-28 리뷰).
            zd = None if z is None else -pos["side"] * z      # 진입 쪽으로 벌어진 정도(+ = 진입 방향)
            if zd is not None and zd > sz:
                why = STOP
            elif zd is not None and zd < xz:
                why = NORMAL
            elif j - pos["ent"] >= time_stop_days(pos["hl"], P):
                why = TIME
            if why and j + 1 < n:
                pend = ("out", why, j)
            elif why:
                end["xsig"] = why
            continue
        bu[j] = wait_beta(j)
        ad = adopt(j)
        if ad is None or j <= cool:
            continue
        b, hl = ad
        z = z_of(j, b)
        if z is None or not (ez < abs(z) < sz):
            continue
        side = SHORT_A if z > 0 else LONG_A
        if not tv_ok(j) or not short_ok(j + 1, side):
            continue
        if j + 1 >= n:
            end["signal"] = (side, b, hl)
        else:
            pend = ("in", side, b, hl, j)
    if pos is not None:
        trades.append(pos)
    end["cool"] = cool
    return trades, bu, end


def weights(beta: float) -> tuple[float, float]:
    ab = abs(beta)
    return 1 / (1 + ab), ab / (1 + ab)


def pnl(tr: dict, cum_a: np.ndarray, cum_b: np.ndarray, dates, cat: dict, last: int | None = None) -> dict:
    """거래 수익(%) — cum_a·cum_b = 공동 거래일 일간 수익률(%) 누적합. 열린 거래는 last(기본 마지막 날)까지 미실현,
    청산 비용(체결·롱 다리 거래세)은 빼지 않는다."""
    P = cat["params"]
    wa, wb = weights(tr["beta"])
    e = tr["ent"]
    x = tr["ext"] if tr["ext"] is not None else (len(cum_a) - 1 if last is None else last)
    g = tr["side"] * (wa * (cum_a[x] - cum_a[e]) - wb * (cum_b[x] - cum_b[e]))
    w_short, w_long = (wb, wa) if tr["side"] == LONG_A else (wa, wb)
    days = x - e
    borrow = w_short * P["borrow_fee_annual"] / P["days_per_year"] * days
    tax = tax_rate(cat, dates[e]) * w_short + (tax_rate(cat, dates[x]) * w_long if tr["ext"] is not None else 0.0)
    sides = 2 if tr["ext"] is not None else 1
    fee = P["commission_slip_per_side"] * sides
    fee_s = P["commission_slip_sens"] * sides
    return {"gross": float(g), "net": float(g - fee - tax - borrow), "net_sens": float(g - fee_s - tax - borrow),
            "days": int(days), "cost": float(fee + tax + borrow)}


# ── 표 ──
def _r(x, d: int = 2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, d) if math.isfinite(x) else None


def trade_stats(trs: list[dict], key: str = "net") -> dict:
    """청산된 거래만 — n·승률(key > 0)·평균·중앙·최악·최선(key), gross 평균·중앙, 평균 보유일, 청산 사유별 건수."""
    c = [t for t in trs if t.get("exit")]
    out = {"n": len(c), "reasons": {r: sum(t["reason"] == r for t in c) for r in REASONS}}
    if not c:
        return out
    v = np.array([t[key] for t in c])
    g = np.array([t["gross"] for t in c])
    out.update(win=_r(100 * (v > 0).mean(), 1), mean=_r(v.mean()), median=_r(np.median(v)), worst=_r(v.min()),
               best=_r(v.max()), mean_gross=_r(g.mean()), median_gross=_r(np.median(g)),
               avg_days=_r(np.mean([t["days"] for t in c]), 1))
    return out


def _bin_label(lo, hi):
    return f"{lo}–{hi}" if hi is not None else f"{lo}+"


def _leg(master: pd.DataFrame, ric: str) -> dict:
    r = master.loc[ric] if ric in master.index else None
    g = (lambda k: None if r is None or k not in r.index or pd.isna(r[k]) else r[k])
    return {"ric": ric, "name": g("name") or ric, "code": g("code")}


def _idx(master: pd.DataFrame, ric: str) -> bool:
    if ric not in master.index or "idx_proxy" not in master.columns:
        return False
    v = master.at[ric, "idx_proxy"]
    return bool(v) if not pd.isna(v) else False


def _badge(rg: str, short_idx: bool | None) -> str:
    if rg == "banned":
        return "불가"
    if rg == "partial":
        return "제한" if short_idx else "불가"
    return "미확인"


# ── 계산 ──
def engine(master: pd.DataFrame, cands: list[dict], ret: pd.DataFrame, tv: pd.DataFrame, cat: dict, today=None) -> dict:
    """원재료 계산(compute 가 표로 만든다) — 테스트가 직접 본다."""
    P = cat["params"]
    ret = ret.copy()
    ret.index = pd.DatetimeIndex(ret.index)
    ret = ret.sort_index()
    ret = ret[~ret.index.duplicated(keep="last")].loc[pd.Timestamp(P["history_start"]):]
    if today is not None:
        ret = ret.loc[:pd.Timestamp(today)]
    ret = ret.dropna(how="all")
    idx = ret.index
    errors, missing = [], []
    ok = []
    for c in cands:
        lack = [x for x in (c["a"], c["b"]) if x not in ret.columns or not ret[x].notna().any()]
        if lack:
            missing.append({"id": c["id"], "reason": "가격 없음(" + ", ".join(lack) + ")"})
        else:
            ok.append(c)
    rics = sorted({c[x] for c in ok for x in ("a", "b")})
    r = ret[rics].astype(float).where(ret[rics] > -100)
    lr = np.log1p(r / 100.0)
    lp = lr.cumsum()                                    # NaN 날은 NaN 으로 남고 누적은 이어진다
    tvx = tv.copy() if tv is not None else pd.DataFrame(index=idx)
    tvx.index = pd.DatetimeIndex(tvx.index)
    tvx = tvx[~tvx.index.duplicated(keep="last")].reindex(index=idx, columns=rics)
    tv20 = tvx.rolling(int(P["tv_window"]), min_periods=int(P["tv_min_periods"])).mean()
    LP, LR, TV = lp.to_numpy(), lr.to_numpy(), tv20.to_numpy()
    col = {x: i for i, x in enumerate(rics)}
    W = int(P["test_window"])
    mes = [i for i in month_ends(idx) if i >= W - 1]
    by_id = {c["id"]: c for c in ok}

    def make(pid):
        c = by_id[pid]
        return Pair(LP[:, col[c["a"]]], LP[:, col[c["b"]]], LR[:, col[c["a"]]], LR[:, col[c["b"]]])

    tests = run_tests({pid: (lambda pid=pid: make(pid)) for pid in by_id}, {c["id"]: c["kind"] for c in ok}, mes, P, errors)
    for c in ok:
        if all(t is None for t in tests[c["id"]]):
            missing.append({"id": c["id"], "reason": f"이력 부족 — 월말 창 {W}행 안 공동 관측 {P['min_obs']} 미만"})
    # 페어별 상태기계
    Wz, mo = int(P["z_window"]), int(P["min_obs"])
    mes_a = np.asarray(mes, int)
    runs = {}
    for c in ok:
        pid = c["id"]
        rs = tests[pid]
        first = next((k for k, t in enumerate(rs) if t and t.get("adopted")), None)
        if first is None:                               # 한 번도 채택 안 됨 — 거래·현재 표 없음
            continue
        pr = make(pid)
        J = np.flatnonzero(pr.m)
        n = len(J)
        dates = idx[J]
        mom = pr.moments(J, Wz)
        nn, ma, mb, va, vb, cab = (x.tolist() for x in mom)     # 상태기계 루프용(파이썬 리스트가 빠르다)
        la, lb = pr.la[J], pr.lb[J]
        cum_a = np.concatenate([[0.0], np.cumsum(np.expm1(np.diff(la)) * 100)])
        cum_b = np.concatenate([[0.0], np.cumsum(np.expm1(np.diff(lb)) * 100)])
        LA, LB = la.tolist(), lb.tolist()
        kk = (np.searchsorted(mes_a, J, side="right") - 1).tolist()     # 그날 유효한 시험(가장 최근 월말 ≤ 그날)
        last_ad, cur = [], None                                          # 시험 k 까지 가장 최근 채택 β
        for t in rs:
            if t and t.get("adopted"):
                cur = t["beta"]
            last_ad.append(cur)
        tva, tvb = TV[J, col[c["a"]]], TV[J, col[c["b"]]]
        tvm = np.fmin(tva, tvb)
        tvm = np.where(np.isnan(tva) | np.isnan(tvb), np.nan, tvm).tolist()
        ia, ib = _idx(master, c["a"]), _idx(master, c["b"])
        nxt = idx[-1] + pd.offsets.BDay(1)

        def z_of(j, b, nn=nn, ma=ma, mb=mb, va=va, vb=vb, cab=cab, LA=LA, LB=LB):
            if b is None or nn[j] < mo:
                return None
            v = va[j] + b * b * vb[j] - 2 * b * cab[j]
            if not v > 0:
                return None
            return ((LA[j] - b * LB[j]) - (ma[j] - b * mb[j])) / math.sqrt(v)

        def adopt(j, kk=kk, rs=rs):
            k = kk[j]
            t = rs[k] if k >= 0 else None
            return (t["beta"], t["hl"]) if t and t.get("adopted") else None

        def wait_beta(j, kk=kk, last_ad=last_ad):
            return last_ad[kk[j]] if kk[j] >= 0 else None

        def short_ok(jf, side, dates=dates, ia=ia, ib=ib, nxt=nxt):
            d = dates[jf] if jf < len(dates) else nxt
            return short_allowed(regime(cat, d), ib if side == LONG_A else ia)

        def tv_ok(j, tvm=tvm):
            return tvm[j] >= P["min_tv_krw"]            # NaN 은 거짓

        start = int(np.searchsorted(J, mes_a[first]))
        trs, bu, end = machine(n, z_of, adopt, wait_beta, short_ok, tv_ok, P, start=start)
        for t in trs:
            t.update(pnl(t, cum_a, cum_b, dates, cat))
            t.update(entry_signal=dates[t["sig"]], entry=dates[t["ent"]],
                     exit_signal=dates[t["xsig"]] if t["xsig"] is not None else None,
                     exit=dates[t["ext"]] if t["ext"] is not None else None,
                     regime_entry=regime(cat, dates[t["ent"]]))
        def z_keep(j, b, mom=mom, la=la, lb=lb):       # 표·차트용 — 리스트 대신 배열을 들고 있는다
            if b is None:
                return None
            nj, maj, mbj, vaj, vbj, cj = (float(x[j]) for x in mom)
            v = vaj + b * b * vbj - 2 * b * cj
            if nj < mo or not v > 0:
                return None
            return ((la[j] - b * lb[j]) - (maj - b * mbj)) / math.sqrt(v)

        runs[pid] = {"J": J, "dates": dates, "trades": trs, "beta_used": bu, "end": end, "z_of": z_keep,
                     "la": la, "lb": lb, "tv_min": tvm[-1] if n else None, "kk": kk}
    return {"idx": idx, "mes": mes, "tests": tests, "runs": runs, "ok": ok, "missing": missing, "errors": errors}


def _row(c: dict, E: dict, master: pd.DataFrame, cat: dict, as_of, next_test) -> dict | None:
    """현재 표 한 줄 — 채택 중·보유 중·최근 recent_months 안 거래가 있는 페어만(아니면 None)."""
    P = cat["params"]
    pid = c["id"]
    run = E["runs"].get(pid)
    if run is None:
        return None
    rs = E["tests"][pid]
    k_last = len(E["mes"]) - 1
    lt = rs[k_last] if k_last >= 0 else None
    adopted = bool(lt and lt.get("adopted"))
    trs, end, n = run["trades"], run["end"], len(run["J"])
    open_tr = trs[-1] if trs and trs[-1]["ext"] is None else None
    since = as_of - pd.DateOffset(months=int(P["recent_months"]))
    recent = any(t["entry_signal"] >= since or (t["exit"] is not None and t["exit"] >= since) for t in trs)
    if not (adopted or open_tr or end["signal"] or recent):
        return None
    j = n - 1
    beta = run["beta_used"][j]
    z = run["z_of"](j, beta) if beta is not None else None
    side, hl_use = None, (lt or {}).get("hl")
    if open_tr:
        state = ST_HOLD if adopted else ST_HOLD_OUT
        side, hl_use = open_tr["side"], open_tr["hl"]
    elif end["signal"]:
        state, side = ST_SIGNAL, end["signal"][0]
    elif j <= end["cool"]:
        state = ST_COOL
    elif adopted:
        state = ST_WAIT
    elif _watch_ok(lt, P):
        return None                                    # 지금은 FDR 만 탈락 — 관찰 줄(_watch_row, 시험 β 의 z)로 보인다
    else:
        state = ST_OUT
    rg = regime(cat, as_of)
    leg_side = side if side is not None else (SHORT_A if z is not None and z > 0 else LONG_A if z is not None and z < 0 else None)
    ia, ib = _idx(master, c["a"]), _idx(master, c["b"])
    short_idx = (ib if leg_side == LONG_A else ia) if leg_side is not None else (ia and ib)
    warns = []
    if open_tr and not adopted:
        warns.append("재검정 탈락 — 보유 중")
    if end["xsig"]:
        warns.append(f"청산 신호({end['xsig']}) — 다음 거래일 체결 예정")
    bw = int(P["beta_drift_window"])
    if beta is not None and n >= bw:
        b63 = _ols(run["la"][-bw:], run["lb"][-bw:])[0]
        if beta != 0 and abs(b63 - beta) / abs(beta) > P["beta_drift_max"]:
            warns.append(f"최근 {bw}일 β 가 시험 β 와 {int(round(P['beta_drift_max'] * 100))}% 넘게 다름(구조변화 의심)")
    tvm = run["tv_min"]
    if not (tvm is not None and tvm >= P["min_tv_krw"]):
        warns.append("거래대금 하한 미달")
    if rg == "partial" or (open_tr and open_tr["regime_entry"] == "partial"):
        warns.append("지수 편입 근사(현재 시총 순위)")
    if run["dates"][-1] < as_of:
        warns.append(f"마지막 공동 거래일 {run['dates'][-1].date()} — 한쪽 거래정지·결측")
    hold = (j - open_tr["ent"]) if open_tr else None
    return {"id": pid, "a": _leg(master, c["a"]), "b": _leg(master, c["b"]), "kind": c["kind"], "pct": c.get("pct"),
            "note": c.get("note") or "",
            "beta": _r(beta, 3), "beta_test": _r((lt or {}).get("beta"), 3),
            "p_ab": _r((lt or {}).get("p_ab"), 4), "p_ba": _r((lt or {}).get("p_ba"), 4), "q": _r((lt or {}).get("q"), 4),
            "half_life": _r((lt or {}).get("hl"), 1), "z": _r(z, 2), "state": state,
            "side": SIDE.get(side), "entry": open_tr["entry"] if open_tr else None, "hold_days": hold,
            "time_stop_days": _r(time_stop_days(hl_use, P), 1) if hl_use is not None and math.isfinite(hl_use) else None,
            "pnl_gross": _r(open_tr["gross"]) if open_tr else None, "pnl_net": _r(open_tr["net"]) if open_tr else None,
            "tv20_min_eok": _r(tvm / 1e8, 1) if tvm is not None else None, "short_badge": _badge(rg, short_idx),
            "tested_at": E["idx"][E["mes"][k_last]] if k_last >= 0 else None, "next_test": next_test,
            "warnings": warns}


def verdict(o: dict, ref: dict | None, P: dict) -> dict:
    """화면 맨 위 한 줄 — 백테스트 표본이 적거나 비용 후 평균이 0 이하면 '신호로 쓰지 않음'(is_signal False)."""
    n, mean = o.get("n", 0), o.get("mean")
    if n < int(P["verdict_min_trades"]):
        text, ok = f"과거 수익 확인 안 됨 — 채택 규칙으로 {n}건만 거래돼 표본이 부족합니다", False
    elif mean is None or mean <= 0:
        text, ok = f"과거 비용 후 평균 {mean}%({n}건) — 신호로 쓰지 않습니다", False
    else:
        text, ok = f"과거 비용 후 거래당 평균 {mean}%({n}건, 승률 {o.get('win')}%)", True
    if ref and ref.get("n"):
        text += (f". 참고로 FDR 문턱만 빼고(양방향 p·반감기·β 조건은 그대로) 돌리면 {ref['n']}건 · 승률 {ref.get('win')}% · "
                 f"비용 후 평균 {ref.get('mean')}%")
    return {"text": text, "is_signal": ok}


def _watch_ok(lt: dict | None, P: dict) -> bool:
    """관찰 조건 — 마지막 시험에서 양방향 p·반감기·β>0 은 통과, 채택(FDR)은 아님."""
    return bool(lt and lt.get("tested") and not lt.get("adopted") and lt["p_ab"] < P["p_max"] and lt["p_ba"] < P["p_max"]
                and lt.get("hl") is not None and P["hl_min"] <= lt["hl"] <= P["hl_max"] and lt["beta"] > 0)


def _watch_row(c: dict, E: dict, master: pd.DataFrame, cat: dict, ret: pd.DataFrame, tv: pd.DataFrame, as_of, next_test) -> dict | None:
    """관찰 — 마지막 월말 시험에서 양방향 p < p_max · 반감기 hl_min~hl_max 는 통과했지만 FDR(q) 에서 떨어진 페어.
    채택이 아니므로 진입·보유·백테스트에 쓰지 않는다. z 는 시험 β 로 같은 창 정의(데이터 인덱스 마지막 z_window 행 안 공동 관측)."""
    P = cat["params"]
    if not E["mes"]:
        return None
    lt = E["tests"][c["id"]][-1]
    if not _watch_ok(lt, P):
        return None
    idx = E["idx"]
    win = idx[-int(P["z_window"]):]
    r = ret.reindex(index=win, columns=[c["a"], c["b"]]).astype(float)
    full = ret.reindex(columns=[c["a"], c["b"]]).astype(float).loc[:idx[-1]]
    lp = np.log1p(full.where(full > -100) / 100).cumsum().reindex(win)
    both = lp.where(r.notna()).dropna()
    z = None
    if len(both) >= int(P["min_obs"]):
        sp = both[c["a"]] - lt["beta"] * both[c["b"]]
        sd = sp.std(ddof=1)
        z = float((sp.iloc[-1] - sp.mean()) / sd) if sd > 0 else None
    tvx = tv.reindex(columns=[c["a"], c["b"]]).astype(float).loc[:idx[-1]].tail(int(P["tv_window"]))
    m = tvx.mean() if tvx.notna().sum().min() >= int(P["tv_min_periods"]) else None
    tvm = float(m.min()) if m is not None else None
    rg = regime(cat, as_of)
    ia, ib = _idx(master, c["a"]), _idx(master, c["b"])
    short_idx = (ia if z > 0 else ib) if z else (ia and ib)
    warns = [f"FDR q {_r(lt['q'], 3)} > {P['fdr_q']} — 채택 아님(신호로 쓰지 않음)"]
    if not (tvm is not None and tvm >= P["min_tv_krw"]):
        warns.append("거래대금 하한 미달")
    return {"id": c["id"], "a": _leg(master, c["a"]), "b": _leg(master, c["b"]), "kind": c["kind"], "pct": c.get("pct"),
            "note": c.get("note") or "", "beta": _r(lt["beta"], 3), "beta_test": _r(lt["beta"], 3),
            "p_ab": _r(lt["p_ab"], 4), "p_ba": _r(lt["p_ba"], 4), "q": _r(lt["q"], 4), "half_life": _r(lt["hl"], 1),
            "z": _r(z, 2), "state": ST_WATCH, "side": None, "entry": None, "hold_days": None,
            "time_stop_days": _r(time_stop_days(lt["hl"], P), 1), "pnl_gross": None, "pnl_net": None,
            "tv20_min_eok": _r(tvm / 1e8, 1) if tvm is not None else None, "short_badge": _badge(rg, short_idx),
            "tested_at": idx[E["mes"][-1]], "next_test": next_test, "warnings": warns}


def _trade_out(t: dict) -> dict:
    return {"entry_signal": t["entry_signal"], "entry": t["entry"], "exit_signal": t["exit_signal"], "exit": t["exit"],
            "side": SIDE[t["side"]], "beta": _r(t["beta"], 3), "half_life": _r(t["hl"], 1), "reason": t["reason"],
            "gross": _r(t["gross"]), "net": _r(t["net"]), "days": t["days"]}


def _chart(run: dict, start) -> dict:
    dates, bu = run["dates"], run["beta_used"]
    js = [j for j in range(len(dates)) if dates[j] >= start]
    z, s, seg = [], [], []
    for j in js:
        b = bu[j]
        z.append(_r(run["z_of"](j, b), 3) if b is not None else None)
        s.append(_r(run["la"][j] - b * run["lb"][j], 4) if b is not None else None)
        if b is not None and seg and seg[-1]["beta_raw"] == b and seg[-1]["j"] == j - 1:
            seg[-1].update(to=dates[j], j=j)
        elif b is not None:
            seg.append({"from": dates[j], "to": dates[j], "beta_raw": b, "j": j})
    trs = [_trade_out(t) for t in run["trades"] if t["exit"] is None or t["exit"] >= start]
    return {"dates": [dates[j] for j in js], "z": z, "s": s,
            "beta": [{"from": g["from"], "to": g["to"], "beta": _r(g["beta_raw"], 4)} for g in seg], "trades": trs}


def compute(master: pd.DataFrame, cands: list[dict], ret: pd.DataFrame, tv: pd.DataFrame, cat: dict | None = None,
            today=None) -> tuple[dict, dict]:
    """순수 계산 → (doc, charts). today = 기준일(그 뒤 데이터는 버린다, 기본 데이터 마지막 날)."""
    cat = cat or load_catalog()
    P = cat["params"]
    E = engine(master, cands, ret, tv, cat, today)
    idx, mes = E["idx"], E["mes"]
    as_of = idx[-1] if len(idx) else None
    by_id = {c["id"]: c for c in E["ok"]}
    next_test = None
    if as_of is not None:
        next_test = last_session(as_of + pd.offsets.MonthBegin(1)) if mes and mes[-1] == len(idx) - 1 \
            else last_session(as_of)
    rows = [r for c in E["ok"] if (r := _row(c, E, master, cat, as_of, next_test))]
    have = {r["id"] for r in rows}
    rows += [r for c in E["ok"] if c["id"] not in have and (r := _watch_row(c, E, master, cat, ret, tv, as_of, next_test))]
    rows.sort(key=lambda r: (STATE_ORDER.index(r["state"]), -abs(r["z"] or 0), r["id"]))
    trades = []
    for pid, run in E["runs"].items():
        for t in run["trades"]:
            trades.append({**t, "id": pid, "kind": by_id[pid]["kind"]})
    closed = [t for t in trades if t["exit"] is not None]
    kinds = cat.get("kinds") or sorted({c["kind"] for c in cands})
    by_year = {}
    for t in closed:
        by_year.setdefault(str(t["entry"].year), []).append(t)
    bins = []
    for lo, hi in P["pct_bins"]:
        ids = [c["id"] for c in E["ok"] if c["kind"] == "모자회사" and c.get("pct") is not None
               and c["pct"] >= lo and (hi is None or c["pct"] < hi)]
        tested = [t for pid in ids for t in E["tests"][pid] if t and t.get("tested")]
        nad = sum(bool(t["adopted"]) for t in tested)
        bins.append({"bin": _bin_label(lo, hi), "lo": lo, "hi": hi, "n_pairs": len(ids), "n_tests": len(tested),
                     "n_adopted": nad, "rate": _r(100 * nad / len(tested), 1) if tested else None})
    k_last = len(mes) - 1
    last = [E["tests"][c["id"]][k_last] for c in E["ok"]] if k_last >= 0 else []
    n_cands = {k: sum(c["kind"] == k for c in cands) for k in kinds}
    meta = {"as_of": as_of, "history_start": P["history_start"],
            "backtest_start": idx[mes[0]] if mes else None,
            "last_test": idx[mes[-1]] if mes else None, "next_test": next_test,
            "params": {k: v for k, v in cat.items() if k not in ("meta", "notes", "definitions")},
            "n_candidates": n_cands, "n_priced": len(E["ok"]),
            "n_tested_last": sum(bool(t and t.get("tested")) for t in last),
            "n_adopted_last": sum(bool(t and t.get("adopted")) for t in last),
            "n_trades": len(closed), "n_open": len(trades) - len(closed),
            "verdict": None, "notes": list(cat.get("notes") or []), "definitions": dict(cat.get("definitions") or {}),
            "errors": E["errors"]}
    ref = None
    if P.get("reference_fdr_q") is not None and P["reference_fdr_q"] != P["fdr_q"]:
        rcat = {**cat, "params": {**P, "fdr_q": P["reference_fdr_q"]}}
        RE = engine(master, cands, ret, tv, rcat, today)
        rc = [t for run in RE["runs"].values() for t in run["trades"] if t["exit"] is not None]
        ref = {"fdr_q": P["reference_fdr_q"], **trade_stats(rc),
               "n_adopted_last": sum(bool(RE["tests"][c["id"]][-1] and RE["tests"][c["id"]][-1].get("adopted"))
                                     for c in RE["ok"]) if RE["mes"] else 0}
    meta_verdict = verdict(trade_stats(closed), ref, P)
    backtest = {"overall": trade_stats(closed), "reference": ref,
                "by_kind": {k: trade_stats([t for t in closed if t["kind"] == k]) for k in kinds},
                "by_year": {y: trade_stats(v) for y, v in sorted(by_year.items())},
                "cost_sens": {"commission_slip_per_side": P["commission_slip_sens"], **trade_stats(closed, "net_sens")},
                "by_pct_bin": bins}
    meta["verdict"] = meta_verdict
    doc = _clean({"generated_at": datetime.datetime.now().isoformat(timespec="seconds"), "meta": meta,
                  "pairs": rows, "backtest": backtest, "missing": E["missing"]})
    start = as_of - pd.DateOffset(years=int(P["chart_years"])) if as_of is not None else None
    charts = _clean({r["id"]: _chart(E["runs"][r["id"]], start) for r in rows if r["id"] in E["runs"]})
    return doc, charts


def _clean(o):
    """JSON 안전값으로(NaN·inf → None, numpy·Timestamp → 기본형) — spikes._clean 과 같은 규칙."""
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


def _save(path, d) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=None, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def build(save: bool = True, refresh: bool = True) -> dict:
    import pair_universe as U    # Refinitiv·DART 를 끌어오므로 여기서만
    m = U.master()
    cands = U.candidates(m, U.dart_links(m))
    rics = sorted({c[x] for c in cands for x in ("a", "b")})
    ret, tv = U.load_prices(rics, refresh=refresh)
    doc, charts = compute(m, cands, ret, tv)
    if save:
        _save(CHARTS, charts)
        _save(OUT, doc)
    return doc


def load() -> dict | None:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_chart(pid: str) -> dict | None:
    try:
        return json.loads(CHARTS.read_text(encoding="utf-8")).get(pid)
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
    d = build(refresh="--offline" not in sys.argv)
    m = d["meta"]
    print(f"기준일 {m['as_of']} · 백테스트 시작 {m['backtest_start']} · 마지막 시험 {m['last_test']}(다음 {m['next_test']})")
    print("후보 " + " · ".join(f"{k} {v}" for k, v in m["n_candidates"].items())
          + f" · 마지막 시험 {m['n_tested_last']} · 채택 {m['n_adopted_last']}")
    o = d["backtest"]["overall"]
    print(f"거래 {o['n']}건 승률 {o.get('win')}% 평균 net {o.get('mean')}% 중앙 {o.get('median')}% "
          f"(gross 평균 {o.get('mean_gross')}%) · 보유 중 {m['n_open']}")
    for r in d["pairs"][:20]:
        print(f"  {r['state']:<10} {r['kind']:<5} {r['a']['name']} / {r['b']['name']} z={r['z']} β={r['beta']} "
              f"HL={r['half_life']} {r['side'] or ''} {'; '.join(r['warnings'])}")
    if d["missing"]:
        print(f"생략 {len(d['missing'])}건")
    for e in m["errors"][:10]:
        print(f"오류: {e}")
    print(OUT)
