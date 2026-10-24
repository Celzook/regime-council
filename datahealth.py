# -*- coding: utf-8 -*-
"""데이터 이상 점검 — 갱신(refresh_all.py) 끝에 돌아 받은 시계열의 마지막 값을 검사한다. 전부 코드 계산.

검사 (마지막 값 기준, 과거 이력의 오류는 보지 않는다):
- stale   : 같은 시장 묶음(국내·미국·기타)의 최신 거래일보다 1영업일 넘게 뒤처짐. KSc1(선물 최근월)은 현물보다 하루 늦게 붙을 때가 있어 info.
- jump    : 마지막 일변화가 직전 1년 일변화 MAD 의 JUMP_K 배 초과 (금리 bp, 가격 %).
- flat    : 평소 움직이는 시계열이 FLAT_RUN 거래일 이상 같은 값 (정지 시세 — 예전 삼성전자 정지값).
- missing : 있어야 할 열이 없거나 최근 1년이 비어 있음, market.json 값이 비어 있음.
결과: data/log/data_health.json (화면 상단 상태 표시용). 찾은 이상이 있어도 갱신 실패로 치지 않는다.

사용법: python datahealth.py
"""
import datetime
import json
import os

import numpy as np
import pandas as pd

import config
from agent import load_json

HIST_DIR = config.DATA_DIR / "history"
OUT = config.DATA_DIR / "log" / "data_health.json"
EQUITY_UNIVERSE = config.BASE_DIR / "universe" / "equity.json"
FILES = ("kofia_daily.csv", "yf_daily.csv", "fred_daily.csv", "equity_daily.csv", "legs_daily.csv")
STALE_BDAYS = {"kr": 1, "us": 1, "other": 2}   # 허용 지연(영업일) — 기타는 나라마다 휴장일이 달라 하루 더
JUMP_K = 10.0       # 1년 MAD 배수 (MAD ≈ 0.67σ → 약 6.7σ). 6배는 지난 1년 92개 시계열에서 하루 2건꼴이라 실제 급변까지 다 걸렸다
FLAT_RUN = 5        # 같은 값 연속 거래일
FLAT_RUN_COL = {"usdcny": 8}   # 춘절·국경절 연휴엔 위안 시세가 5~7일 멈춘다(2026-02·10월 실측)
FLAT_ZERO_MAX = 0.1  # 직전 1년 무변화일 비중이 이보다 높으면 원래 잘 안 움직이는 시계열(CD·기준금리)로 보고 건너뜀
WINDOW = 252
LAG_INFO = {"ks200_fut", "KSc1"}

# 시장 묶음 — 열 이름 규칙으로 가른다(국내: KOFIA·.KS·원화, 기타: 일본·유럽·중화권·인도)
KR_COLS = {"kospi", "kosdaq", "kospi200", "ks200_fut", "usdkrw", "KRW=X", "JPYKRW=X", "^KS11", "^KQ11", "^KS200", ".KSVKOSPI"}
OTHER_COLS = {"nikkei", "stoxx50", "^N225", "^STOXX50E", "^HSI", "^NSEI", "^TWII", ".V2TX"}
RATE_COLS = {"ust_10y", "^IRX"}   # 파일 밖에서 금리인 열 (KOFIA·FRED 는 파일째 금리)


def _group(file: str, col: str) -> str:
    if file.startswith("kofia") or col in KR_COLS or col.endswith((".KS", ".KQ")):
        return "kr"
    if col in OTHER_COLS or col.endswith((".SS", ".SZ", ".HK", ".T")):
        return "other"
    return "us"


def _is_rate(file: str, col: str) -> bool:
    return file.startswith(("kofia", "fred")) or col in RATE_COLS


def _labels() -> dict:
    """열 → 표시명. analytics.SERIES(매크로) + universe/equity.json(주식 티커)."""
    import analytics
    lab = {k: v[0] for k, v in analytics.SERIES.items()}
    eq = load_json(EQUITY_UNIVERSE) or {}
    for s in eq.get("series", []):
        lab.setdefault(s["ticker"], s["name"])
    return lab


def _bdays(a, b) -> int:
    return int(np.busday_count(pd.Timestamp(a).date(), pd.Timestamp(b).date()))


def check_series(s: pd.Series, rate: bool, flat_run: int = FLAT_RUN) -> list[tuple[str, str]]:
    """한 시계열의 jump·flat 검사 → [(kind, detail)]. s 는 날짜 인덱스, 빈칸 포함 가능."""
    s = s.dropna()
    out = []
    if len(s) < 60:
        return out
    ch = (s.diff() * 100) if rate else (s.pct_change() * 100)
    ch = ch.dropna()
    unit = "bp" if rate else "%"
    hist, last = ch.iloc[-WINDOW - 1:-1], ch.iloc[-1]
    med = hist.median()
    mad = (hist - med).abs().median()
    if mad > 0 and abs(last - med) > JUMP_K * mad:
        out.append(("jump", f"{s.index[-1]:%m-%d} 일변화 {last:+.2f}{unit} — 1년 MAD {mad:.2f}{unit} 의 {abs(last - med) / mad:.0f}배"))
    run = int((s.iloc[::-1] == s.iloc[-1]).cumprod().sum())
    zero_share = float((hist == 0).mean())
    if run >= flat_run and zero_share < FLAT_ZERO_MAX:
        out.append(("flat", f"{run}거래일 연속 같은 값 {s.iloc[-1]:g} ({s.index[-run]:%m-%d}~) — 시세 정지 의심"))
    return out


def check_stale(last: dict, lag_info=LAG_INFO) -> list[tuple[str, str, str, str]]:
    """last = {(file, col): 마지막 유효일} → [(file, col, severity, detail)].
    기준일 = 같은 시장 묶음에서 시계열 2개 이상이 도달한 가장 늦은 날 (장중에 받은 VIX 하나가 기준을 끌어올리지 않게)."""
    dates = {}
    for (f, c), d in last.items():
        dates.setdefault(_group(f, c), []).append(d)
    ref = {g: (sorted(ds)[-2] if len(ds) > 1 else ds[0]) for g, ds in dates.items()}
    out = []
    for (f, c), d in last.items():
        g = _group(f, c)
        lag = _bdays(d, ref[g])
        if lag > STALE_BDAYS[g] or (c in lag_info and lag >= 1):
            sev = "info" if c in lag_info else "warn"
            out.append((f, c, sev, f"마지막 {d:%Y-%m-%d} — {'국내' if g == 'kr' else '미국' if g == 'us' else '해외'} 최신 {ref[g]:%m-%d} 보다 {lag}영업일 늦음"))
    return out


def run() -> dict:
    labels = _labels()
    issues, last, counted = [], {}, 0

    def add(key, label, kind, sev, detail):
        issues.append({"key": key, "label": label, "kind": kind, "severity": sev, "detail": detail})

    cols_seen = set()
    year_ago = pd.Timestamp.today().normalize() - pd.Timedelta(days=365)
    for fn in FILES:
        p = HIST_DIR / fn
        if not p.exists():
            add(fn, fn, "missing", "warn", "시계열 파일 없음")
            continue
        df = pd.read_csv(p, index_col=0, parse_dates=True).sort_index()
        stem = fn.removesuffix("_daily.csv")
        cols_seen |= set(df.columns)
        for c in df.columns:
            s = df[c]
            key, label = f"{stem}:{c}", labels.get(c, c)
            if s[s.index >= year_ago].dropna().empty:
                add(key, label, "missing", "warn", f"최근 1년 값 없음 ({fn})")
                continue
            counted += 1
            last[(stem, c)] = s.last_valid_index()
            for kind, detail in check_series(s, _is_rate(fn, c), FLAT_RUN_COL.get(c, FLAT_RUN)):
                add(key, label, kind, "warn", f"{detail} ({fn})")
    for f, c, sev, detail in check_stale(last):
        add(f"{f}:{c}", labels.get(c, c), "stale", sev, f"{detail} ({f}_daily.csv)")

    # 있어야 할 열: analytics 기본 시계열(파생 제외) + 주식 유니버스 티커
    import analytics
    expected = [k for k in analytics.SERIES if k not in analytics.DERIVED and k not in analytics.RATIO]
    eq = load_json(EQUITY_UNIVERSE) or {}
    expected += [s["ticker"] for s in eq.get("series", []) if s.get("ticker")]
    for c in expected:
        if c not in cols_seen:
            add(f"expected:{c}", labels.get(c, c), "missing", "warn", "수집 파일에 열이 없음")

    # 시장 스냅샷: 빈 값·stale_fields
    m = load_json(config.DATA_DIR / "market.json") or {}
    if not m:
        add("market", "시장 스냅샷", "missing", "warn", "data/market.json 없음")
    for f in (m.get("meta") or {}).get("stale_fields") or []:
        add(f"market:{f}", f, "stale", "warn", "market.json 이 오래된 값으로 표시")
    for sec, items in m.items():
        if sec == "meta" or not isinstance(items, dict):
            continue
        for k, v in items.items():
            counted += 1
            if not isinstance(v, dict) or v.get("value") is None:
                add(f"market:{sec}.{k}", labels.get(k, k), "missing", "warn", "market.json 값 비어 있음")

    order = {"warn": 0, "info": 1}
    issues.sort(key=lambda r: (order[r["severity"]], r["kind"], r["key"]))
    res = {"checked_at": datetime.datetime.now().isoformat(timespec="seconds"), "issues": issues, "counted": counted}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT)
    return res


if __name__ == "__main__":
    r = run()
    print(f"점검 {r['counted']}개 · 이상 {len(r['issues'])}건")
    for i in r["issues"]:
        print(f"[{i['severity']}] {i['kind']:7} {i['label']} — {i['detail']}")
