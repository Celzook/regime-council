# -*- coding: utf-8 -*-
"""과거 사례 점검 — 유사 국면(analog) 탐색. 전부 코드 계산, LLM 은 결과 해석만.

방법:
1. 자산별 '국면 특징 벡터'를 일별로 계산하고 전 기간 z-점수로 표준화.
2. 현재 벡터와의 유클리드 거리로 과거 유사일을 찾는다 (최근 N개월 제외, 인접일 중복 제거).
3. 유사일 이후 3/6/12개월 결과(금리 bp 변화·지수 수익률·환율 변화율)와
   구간 내 최대 역행폭(손실 요인의 계량 표현)을 집계한다.
"""
import datetime
import math

import numpy as np
import pandas as pd

import config

HIST_DIR = config.DATA_DIR / "history"
HORIZONS = {"3m": 63, "6m": 126, "12m": 252}
ASSET_NAMES = {"bond": "채권", "equity": "주식", "fx": "환"}


def load_history() -> pd.DataFrame:
    k = pd.read_csv(HIST_DIR / "kofia_daily.csv", index_col=0, parse_dates=True)
    y_path = HIST_DIR / "yf_daily.csv"
    y = pd.read_csv(y_path, index_col=0, parse_dates=True) if y_path.exists() else pd.DataFrame()
    df = k.join(y, how="outer").sort_index()
    df = df.ffill(limit=5)            # 휴장일 불일치 메우기 (최대 5영업일)
    return df[df.index >= "2012-01-01"]


# ── 특징·결과 정의 ────────────────────────────────────────────────
def _features(df: pd.DataFrame, asset: str) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    if asset == "bond":
        f["국고3y 레벨(%)"] = df["ktb_3y"]
        f["국고3y 3개월 변화(bp)"] = (df["ktb_3y"] - df["ktb_3y"].shift(63)) * 100
        f["커브 3s10s(bp)"] = (df["ktb_10y"] - df["ktb_3y"]) * 100
        f["AA- 스프레드(bp)"] = (df["corp_aa_minus_3y"] - df["ktb_3y"]) * 100
        if "ust_10y" in df:
            f["미국채10y 3개월 변화(bp)"] = (df["ust_10y"] - df["ust_10y"].shift(63)) * 100
    elif asset == "equity":
        f["KOSPI 3개월 수익률(%)"] = df["kospi"].pct_change(63) * 100
        f["KOSPI 6개월 수익률(%)"] = df["kospi"].pct_change(126) * 100
        f["VIX 레벨"] = df["vix"]
        f["KOSPI-S&P 3개월 상대(%p)"] = (df["kospi"].pct_change(63) - df["sp500"].pct_change(63)) * 100
        f["국고3y 3개월 변화(bp)"] = (df["ktb_3y"] - df["ktb_3y"].shift(63)) * 100
    elif asset == "fx":
        roll = df["usdkrw"].rolling(252)
        f["USD/KRW 1년평균 대비 z"] = (df["usdkrw"] - roll.mean()) / roll.std()
        f["USD/KRW 3개월 변화(%)"] = df["usdkrw"].pct_change(63) * 100
        f["DXY 3개월 변화(%)"] = df["dxy"].pct_change(63) * 100
        f["한미 10y 금리차(bp)"] = (df["ktb_10y"] - df["ust_10y"]) * 100
        f["VIX 레벨"] = df["vix"]
    return f


def _outcomes(df: pd.DataFrame, asset: str) -> dict[str, tuple]:
    """결과 변수. (시계열, adverse_up, kind) — kind 'diff' 는 차이(bp), 'pct' 는 단순 수익률(%)."""
    if asset == "bond":
        return {"국고3y 변화(bp)": (df["ktb_3y"] * 100, True, "diff"),
                "국고10y 변화(bp)": (df["ktb_10y"] * 100, True, "diff"),
                "AA- 스프레드 변화(bp)": ((df["corp_aa_minus_3y"] - df["ktb_3y"]) * 100, True, "diff")}
    if asset == "equity":
        return {"KOSPI 수익률(%)": (df["kospi"], False, "pct"),
                "S&P500 수익률(%)": (df["sp500"], False, "pct")}
    return {"USD/KRW 변화(%)": (df["usdkrw"], False, "pct")}  # 하락(원화 강세) = 환오픈 손실


def _change(series: pd.Series, i0: int, i1: int, kind: str):
    s0, s1 = series.iloc[i0], series.iloc[i1]
    if pd.isna(s0) or pd.isna(s1) or (kind == "pct" and s0 == 0):
        return None
    return round(float((s1 / s0 - 1) * 100 if kind == "pct" else s1 - s0), 1)


def _window_move(win: pd.Series, adverse_up: bool, kind: str):
    """구간 내 불리한 방향 최대 이동."""
    win = win.dropna()
    if len(win) < 2:
        return None
    base = win.iloc[0]
    if kind == "pct":
        if base == 0:
            return None
        return round(float(((win.max() if adverse_up else win.min()) / base - 1) * 100), 1)
    return round(float((win.max() if adverse_up else win.min()) - base), 1)


def find_analogs(asset: str, k: int = 10, exclude_months: int = 12,
                 min_gap_days: int = 126, as_of: str | None = None) -> dict:
    df = load_history()
    if as_of:
        df = df[df.index <= pd.Timestamp(as_of)]  # 기준일 이후 데이터는 결과 계산에서도 차단
    feats = _features(df, asset)
    valid = feats.dropna()
    if valid.empty:
        raise RuntimeError(f"{asset}: 특징 계산 가능한 데이터가 없습니다.")
    current_date = valid.index[-1]
    cur = valid.loc[current_date]

    # robust z (중앙값·MAD), ±3 윈저라이즈 — 극단치가 척도를 왜곡하지 않게
    med = valid.median()
    mad = (valid - med).abs().median() * 1.4826
    mad = mad.replace(0, np.nan)
    z = ((valid - med) / mad).clip(-3, 3)
    zc = ((cur - med) / mad).clip(-3, 3)
    cutoff = current_date - pd.DateOffset(months=exclude_months)
    cand = z[z.index < cutoff]
    dist = np.sqrt(((cand - zc) ** 2).sum(axis=1)).sort_values()

    picked = []
    for d, dv in dist.items():
        if all(abs((d - p).days) >= min_gap_days for p, _ in picked):
            picked.append((d, float(dv)))
        if len(picked) >= k:
            break

    # 독립 사례 수: 12개월 결과 창(252영업일)이 겹치지 않는 사례만 센다
    independent = []
    for d, _ in sorted(picked):
        if all(abs((d - p).days) >= 365 for p in independent):
            independent.append(d)
    years = sorted({d.year for d, _ in picked})
    flags = []
    if len(independent) < 5:
        flags.append("LOW_SAMPLE")
    if picked:
        pct = [float((dist < dv).mean() * 100) for _, dv in picked]
        if np.median(pct) > 5:
            flags.append("WEAK_SIMILARITY")
    else:
        pct = []
    if picked and len(years) <= 2:
        flags.append("SINGLE_REGIME")
    if asset == "bond":
        flags.append("SHORT_HISTORY")  # 2012~ : 금리 인상기는 2022년 1회뿐

    outs = _outcomes(df, asset)
    last_date = df.index[-1]
    analogs = []
    for i, (d, dv) in enumerate(picked):
        pos = df.index.get_indexer([d], method="nearest")[0]
        rec = {"date": d.date().isoformat(), "distance": round(dv, 2),
               "distance_pctile": round(pct[i], 1),
               "features": {c: round(float(feats.loc[d, c]), 2) for c in feats.columns},
               "outcomes": {}, "max_adverse": {}, "path": {}}
        # 경로 지표(6개월): 손실 요인의 계량 표현 — 서술 없이 숫자만
        end6 = min(pos + HORIZONS["6m"], len(df.index) - 1)
        seg = df.iloc[pos:end6 + 1]
        if len(seg) > 1:
            k0 = seg["kospi"].dropna() if "kospi" in seg else pd.Series(dtype=float)
            if len(k0) > 1:
                rec["path"]["KOSPI MDD(%)"] = round(float(((k0 / k0.cummax()) - 1).min() * 100), 1)
            if "vix" in seg and seg["vix"].notna().any():
                rec["path"]["VIX 최고"] = round(float(seg["vix"].max()), 1)
            r3 = seg["ktb_3y"].dropna() if "ktb_3y" in seg else pd.Series(dtype=float)
            if len(r3) > 1:
                rec["path"]["국고3y 최대 상승(bp)"] = round(float((r3.max() - r3.iloc[0]) * 100), 1)
            u = seg["usdkrw"].dropna() if "usdkrw" in seg else pd.Series(dtype=float)
            krw_weak = krw_strong = None
            if len(u) > 1 and u.iloc[0]:
                krw_weak = round(float((u.max() / u.iloc[0] - 1) * 100), 1)     # 원화 약세 최대(%)
                krw_strong = round(float((u.min() / u.iloc[0] - 1) * 100), 1)   # 원화 강세 최대(%)
                rec["path"]["원화 약세 최대(%)"] = krw_weak
                rec["path"]["원화 강세 최대(%)"] = krw_strong
            # 시장 기준 리스크오프 동시성(금리↑·주가↓·원화 약세) vs 포트 기준 동시 역행(금리↑·주가↓·환오픈 손실=원화 강세)
            up = rec["path"].get("국고3y 최대 상승(bp)", 0) >= 50
            dd = rec["path"].get("KOSPI MDD(%)", 0) <= -10
            rec["path"]["리스크오프 동시성(시장: 금리↑50bp·주가↓10%·원화약세5%)"] = int(up) + int(dd) + int((krw_weak or 0) >= 5)
            rec["path"]["동시 역행 수(포트: 금리↑50bp·주가↓10%·원화강세5%)"] = int(up) + int(dd) + int((krw_strong or 0) <= -5)
        for hname, hdays in HORIZONS.items():
            end_pos = pos + hdays
            if end_pos >= len(df.index) or df.index[end_pos] > last_date:
                rec["outcomes"][hname] = None
                continue
            rec["outcomes"][hname] = {oname: _change(series, pos, end_pos, kind) for oname, (series, _, kind) in outs.items()}
        # 6개월 내 최대 역행폭: 포트에 불리한 방향으로 가장 많이 움직인 폭
        end_pos = min(pos + HORIZONS["6m"], len(df.index) - 1)
        for oname, (series, adverse_up, kind) in outs.items():
            mv = _window_move(series.iloc[pos:end_pos + 1], adverse_up, kind)
            if mv is not None:
                rec["max_adverse"][oname] = mv
        analogs.append(rec)

    summary = {}
    for hname in HORIZONS:
        rows = [a["outcomes"][hname] for a in analogs if a["outcomes"].get(hname)]
        if not rows:
            continue
        summary[hname] = {}
        for oname, (_, adverse_up, _kind) in outs.items():
            vals = [r[oname] for r in rows if r.get(oname) is not None]
            if not vals:
                continue
            arr = np.array(vals)
            summary[hname][oname] = {
                "n": len(vals), "n_independent": len(independent), "median": round(float(np.median(arr)), 1),
                "p25": round(float(np.percentile(arr, 25)), 1),
                "p75": round(float(np.percentile(arr, 75)), 1),
                "worst": round(float(arr.max() if adverse_up else arr.min()), 1),
                "adverse_direction": "상승" if adverse_up else "하락",
            }

    caveats = [
        f"표본 {len(analogs)}개(12개월 창이 겹치지 않는 독립 사례 {len(independent)}개, 연도 {', '.join(map(str, years))}) — 통계적 분포가 아니라 참고 사례다. '과거가 반복된다'고 해석하지 말 것.",
        f"탐색 기간 {valid.index[0].date()} ~ {cutoff.date()} (최근 {exclude_months}개월 제외), 후보 {len(cand)}일, 유사일 간 최소 간격 {min_gap_days}영업일.",
        "거리 = robust z(중앙값·MAD) 공간의 유클리드 거리, 백분위 = 전체 후보 중 가까운 순위(%). 백분위 중앙값이 5% 를 넘으면 WEAK_SIMILARITY.",
        "주식·환 결과는 단순 수익률(%), 금리·스프레드는 bp 차이 — 스트레스 시나리오(주식 −25%, 원화 ±8%)와 같은 단위.",
        "구조 변화(통화정책 체계·시장 규모·규제)로 과거 반응이 재현되지 않을 수 있음.",
    ]
    if "LOW_SAMPLE" in flags:
        caveats.append("LOW_SAMPLE: 독립 사례 5건 미만 — 분포 통계로 추론하지 말고 사례 나열로만 참고.")
    if "SINGLE_REGIME" in flags:
        caveats.append("SINGLE_REGIME: 유사일이 1~2개 연도에 집중 — 사실상 한 국면의 반복 표본.")
    if "WEAK_SIMILARITY" in flags:
        caveats.append("WEAK_SIMILARITY: 현재 국면과 충분히 비슷한 과거가 없음 — 결과 분포를 근거로 쓰지 말 것.")
    if asset == "bond":
        caveats.append("SHORT_HISTORY: 국고·회사채 시계열은 2012년부터 — 금리 인상기는 2022년 1회뿐이고 2008년형 신용 위기 사례는 없음.")
    return {
        "asset": asset, "asset_name": ASSET_NAMES[asset],
        "as_of": current_date.date().isoformat(),
        "current_features": {c: round(float(v), 2) for c, v in cur.items()},
        "search": {"universe_start": valid.index[0].date().isoformat(),
                   "universe_end": cutoff.date().isoformat(), "n_candidates": int(len(cand)),
                   "k": k, "exclude_months": exclude_months, "dedup_days": min_gap_days,
                   "metric": "robust-z euclid"},
        "flags": flags, "n_independent": len(independent), "years": years,
        "analogs": analogs, "summary": summary, "caveats": caveats,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def to_markdown(res: dict) -> str:
    s = res.get("search", {})
    lines = [f"### 과거 유사 국면 — {res['asset_name']} (기준일 {res['as_of']})", "",
             f"플래그: {', '.join(res.get('flags') or ['없음'])} · 표본 {len(res['analogs'])}건 · "
             f"탐색 {s.get('universe_start')}~{s.get('universe_end')} (후보 {s.get('n_candidates')}일)", "",
             "**현재 특징**: " + ", ".join(f"{k} {v}" for k, v in res["current_features"].items()), "",
             "| 유사일 | 거리 | 백분위(%) | " + " | ".join(res["current_features"].keys()) + " |",
             "|---|---|---|" + "---|" * len(res["current_features"])]
    for a in res["analogs"]:
        lines.append(f"| {a['date']} | {a['distance']} | {a.get('distance_pctile', '')} | "
                     + " | ".join(str(a["features"][c]) for c in res["current_features"]) + " |")
    for hname, d in res["summary"].items():
        first = next(iter(d.values()))
        lines += ["", f"**이후 {hname} 결과 분포 (n={first['n']}, 독립 사례 {first.get('n_independent', '-')})**",
                  "| 지표 | 중앙값 | 25% | 75% | 최악 | 불리한 방향 |", "|---|---|---|---|---|---|"]
        for oname, s in d.items():
            lines.append(f"| {oname} | {s['median']} | {s['p25']} | {s['p75']} | {s['worst']} | {s['adverse_direction']} |")
    if res["analogs"] and res["analogs"][0]["max_adverse"]:
        lines += ["", "**유사일 이후 6개월 내 최대 역행폭 (불리한 방향)**",
                  "| 유사일 | " + " | ".join(res["analogs"][0]["max_adverse"].keys()) + " |",
                  "|---|" + "---|" * len(res["analogs"][0]["max_adverse"])]
        for a in res["analogs"]:
            lines.append(f"| {a['date']} | " + " | ".join(str(v) for v in a["max_adverse"].values()) + " |")
    if res["analogs"] and res["analogs"][0].get("path"):
        keys = list(res["analogs"][0]["path"].keys())
        lines += ["", "**당시 손실 요인 (유사일 이후 6개월 경로 지표)**",
                  "| 유사일 | " + " | ".join(keys) + " |", "|---|" + "---|" * len(keys)]
        for a in res["analogs"]:
            lines.append(f"| {a['date']} | " + " | ".join(str(a["path"].get(k_, "")) for k_ in keys) + " |")
    lines += ["", "**유의사항**"] + [f"- {c}" for c in res["caveats"]]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asset = sys.argv[1] if len(sys.argv) > 1 else "bond"
    print(to_markdown(find_analogs(asset)))
