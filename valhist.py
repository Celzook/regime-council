# -*- coding: utf-8 -*-
"""주식 유니버스 밸류에이션 이력 — 항목마다 '자기' 과거 PER·PBR 월말 값(2016-01~). 지금 값이 자기 범위 어디쯤인지 보는 데 쓴다.

출처: Refinitiv(LSEG) Workspace(refinitiv_src). 종목 월말 TR.PE·TR.PriceToBVPerShare·TR.PriceClose(수정주가)
      — {"SDate", "EDate", "Frq": "M"}. 이번 달은 아직 월말이 없어 지금 값(스냅샷)을 넣는다.
      지수·ETF 자체는 이 필드가 비어서(2026-10-24 실측) 구성 종목으로 묶는다.
계산(CODE):
  종목 항목 = 그 종목 자기 값.
  ETF 항목 = '지금' 구성 종목·비중(fundamentals.etfs) 중 비중 큰 순으로 누적 90%(최대 300종목)까지.
            월 t 비중 w_i(t) = 지금 비중 × P_i(t) ÷ P_i(지금) — 주식 수를 고정하고 가격만 되돌린 근사.
            묶음 PER = Σw ÷ Σ(w ÷ PER) (fundamentals.basket 과 같은 조화평균, PER 이 0 이하·없음이면 뺀다). PBR 도 같다.
            가격이 있는 종목의 지금 비중 합이 고른 비중의 절반이 안 되는 달은 비운다.
            PER(PBR)이 쓸 만한 종목(가격 있고 PER ≥ 1 / PBR ≥ 0.05)의 지금 비중 합이 고른 비중의 60% 가 안 되는 달도
            그 지표만 비운다 — 적자 종목이 대부분인 달(2020~21 미국 에너지, 바이오 많은 코스닥·국내 헬스케어)의 묶음 PER 은
            흑자 소수 종목 값일 뿐이라서. 달마다 그 비율을 pe_cov·pb_cov(%)로 낸다.
            종목 값 정리: PER < 1·PBR < 0.05 는 원천 오류로 뺀다(fundamentals.sane). 우선주(fundamentals.PREF)는
            Refinitiv 가 보통주 PER·PBR 을 주므로 그 달 '우선주 가격 ÷ 보통주 가격'을 곱한다.
  한계: 과거 구성 변경·상장폐지 종목이 빠진다(현재 구성 고정 근사, 생존 편향).
캐시: data/history/val_monthly.csv(종목×월 원천 값 — 저장소 제외, snap=1 은 이번 달 스냅샷 행).
      월말 이력을 한 번이라도 받은 종목(snap 아닌 행이 있는 종목)만 '있던 종목' — 그 마지막 월말부터 다시 받는다(시작 달이 같은 종목끼리 묶어서).
      이력을 못 받은 종목은 스냅샷 행만 있어도 다음에 처음부터 받는다. 받기에 실패한 묶음은 출력 meta.partial·skipped 에 남긴다.
출력: data/equity_valhist.json — 주식 탭 '자기 과거 대비' 화면이 쓴다. 월 단위라 7일 지나면 다시 받는다.

사용법: python valhist.py            # 증분
       python valhist.py --full     # 캐시 무시하고 처음부터
"""
import datetime
import json
import os
import sys
import time
import warnings

import pandas as pd

import config
import fundamentals as F
import refinitiv_src as R

OUT = config.DATA_DIR / "equity_valhist.json"
CACHE = config.DATA_DIR / "history" / "val_monthly.csv"
START = "2016-01-01"
STALE_DAYS = 7
CUM_W, MAX_N, BATCH = 90.0, 300, 100
MIN_SHARE = 0.5          # 그 달 가격이 있는 종목의 비중 합 ÷ 고른 비중 — 이보다 작으면 비운다
MIN_VALID = 0.6          # 그 달 PER(PBR)이 쓸 만한 종목의 비중 합 ÷ 고른 비중 — 이보다 작으면 그 지표를 비운다
HIST_FIELDS = ["TR.PriceClose.date", "TR.PriceClose", "TR.PE", "TR.PriceToBVPerShare"]
NOW_FIELDS = ["TR.PriceClose", "TR.PE", "TR.PriceToBVPerShare"]
METHOD = ("월말 PER·PBR(최근 실적, Refinitiv). 종목은 자기 값, ETF·지수는 지금 구성 종목 중 비중 상위(누적 90%, 최대 300종목)를 "
          "지금 비중에서 가격 변화만큼 되돌린 비중으로 조화평균(적자·PER 없는 종목 제외). 이번 달은 지금 값. "
          "PER(PBR)이 있는 종목 비중이 60% 미만인 달은 비웁니다. 우선주는 보통주 값 × 가격 비율. "
          "과거 구성 변경·상장폐지 종목은 반영하지 못합니다(현재 구성·비중 고정 근사, 생존 편향).")


# ── 계산(네트워크 없음) ──
def pick(holdings: list, cum: float = CUM_W, cap: int = MAX_N) -> tuple[list, float]:
    """비중 큰 순으로 누적 cum% 까지(최대 cap 개) — ([(code, w)…], 고른 비중 ÷ 전체 %)."""
    hs = sorted(((h["code"], h["w"]) for h in holdings if h.get("w")), key=lambda x: -x[1])
    total = sum(w for _, w in hs)
    out, acc = [], 0.0
    for c, w in hs[:cap]:
        if acc >= cum / 100 * total:
            break
        out.append((c, w))
        acc += w
    return out, (acc / total * 100 if total else 0.0)


def harmonic(pairs) -> float | None:
    """Σw ÷ Σ(w ÷ x) — x 가 없거나 0 이하면 뺀다(fundamentals.basket 과 같음)."""
    ws = [(w, x) for w, x in pairs if w and w > 0 and x is not None and x == x and x > 0]
    return sum(w for w, _ in ws) / sum(w / x for w, x in ws) if ws else None


def basket_month(picked: list, row: dict, now_price: dict) -> tuple:
    """한 달 묶음 (PER, PBR, PER 쓸 만한 비중 %, PBR 쓸 만한 비중 %). row = {code: (price, pe, pb)}, w_i(t) = w_i × P_i(t) ÷ P_i(지금).
    비중 % 는 지금 비중 기준(가격 있고 값 > 0 인 종목) — MIN_VALID 미만이면 그 지표는 None."""
    tw = sum(w for _, w in picked)
    ws = []
    for c, w in picked:
        p, pe, pb = row.get(c, (None, None, None))
        p0 = now_price.get(c)
        if p and p0 and p > 0 and p0 > 0:
            ws.append((w * p / p0, pe, pb, w))
    if not tw:
        return None, None, None, None
    ok = lambda x: x is not None and x == x and x > 0  # noqa: E731
    pe_cov = sum(w0 for _, pe, _, w0 in ws if ok(pe)) / tw * 100
    pb_cov = sum(w0 for _, _, pb, w0 in ws if ok(pb)) / tw * 100
    if sum(x[3] for x in ws) < MIN_SHARE * tw:
        return None, None, pe_cov, pb_cov
    pe = harmonic((w, pe) for w, pe, _, _ in ws) if pe_cov >= MIN_VALID * 100 else None
    pb = harmonic((w, pb) for w, _, pb, _ in ws) if pb_cov >= MIN_VALID * 100 else None
    return pe, pb, pe_cov, pb_cov


def clean_row(row: dict) -> dict:
    """한 달 {ric: (price, pe, pb)} 정리 — 우선주는 보통주 값 × 가격 비율, 그다음 PER < 1·PBR < 0.05 는 None."""
    out = dict(row)
    for pref, com in F.PREF.items():
        if pref in row and com in row:
            p, _, _ = row[pref]
            pc, pe_c, pb_c = row[com]
            k = p / pc if p and pc else None
            out[pref] = (p, pe_c * k if k and pe_c is not None else None, pb_c * k if k and pb_c is not None else None)
        elif pref in row:   # 보통주 값이 없으면 보통주 값인 원천을 쓰지 않는다
            out[pref] = (row[pref][0], None, None)
    return {c: (p, *F.sane(pe, pb)) for c, (p, pe, pb) in out.items()}


def position(item: dict | None, m: str = "pe", n: int = 60) -> tuple[float, int] | None:
    """(현재 값, 최근 n 개월 중 백분위 %) — 화면 valStat 과 같은 계산(현재 = 값이 있는 마지막 달, 낮았던 달 + 같은 달 ½). 자료 6개월 미만이면 None."""
    ok = [v for v in ((item or {}).get(m) or [])[-n:] if v is not None and v > 0]
    if len(ok) < min(6, n / 2):
        return None
    cur = ok[-1]
    return cur, round(100 * (sum(v < cur for v in ok) + 0.5 * sum(v == cur for v in ok)) / len(ok))


def position_text(d: dict | None, key: str) -> str:
    """에이전트 컨텍스트 한 칸 — 'PER 29.4배(구성 상위 90% 기준) 5년 중 61% · PBR 5년 중 80%'. 이력이 없으면 '-'."""
    it = ((d or {}).get("items") or {}).get(key)
    pe, pb = position(it, "pe"), position(it, "pb")
    if not pe and not pb:
        return "-"
    basis = "(구성 상위 90% 기준)" if (it.get("n") or 1) > 1 else ""
    parts = [f"PER {pe[0]:.1f}배{basis} 5년 중 {pe[1]}%"] if pe else []
    parts += [f"PBR {pb[0]:.2f}배 5년 중 {pb[1]}%"] if pb else []
    return " · ".join(parts)


def months(start: str, today: datetime.date) -> list[str]:
    return [p.strftime("%Y-%m") for p in pd.period_range(start[:7], today.strftime("%Y-%m"), freq="M")]


# ── 받아오기 ──
def _num(v):
    return F._f(v)


def _fetch_hist(rics: list[str], sdate: str, edate: str, skipped: list | None = None) -> list[tuple]:
    """월말 원천 값 — [(ric, YYYY-MM, price, pe, pb)…]. BATCH 개씩. 실패한 묶음의 종목은 skipped 에 더한다."""
    out = []
    for i in range(0, len(rics), BATCH):
        part = rics[i:i + BATCH]
        try:
            df, _ = R.snapshot(part, HIST_FIELDS, {"SDate": sdate, "EDate": edate, "Frq": "M"})
        except Exception as e:  # noqa: BLE001 — 한 묶음 실패는 건너뛰고 다음 실행 때 다시 받는다
            print(f"  [건너뜀] {part[0]}… {len(part)}종목: {e}")
            if skipped is not None:
                skipped += part
            continue
        for r in df.itertuples(index=False):
            d = str(r[1])[:7]
            if len(d) == 7 and d[4] == "-":
                out.append((str(r[0]), d, _num(r[2]), _num(r[3]), _num(r[4])))
        print(f"  월말 값 {min(i + BATCH, len(rics))}/{len(rics)}")
    return out


COLS = ["ric", "month", "price", "pe", "pb", "snap"]


def _load_cache() -> pd.DataFrame:
    try:
        df = pd.read_csv(CACHE, dtype={"ric": str, "month": str})
    except (OSError, ValueError):
        return pd.DataFrame(columns=COLS)
    if "snap" not in df:          # snap 열 이전 캐시 — 지난달 이전 행은 모두 월말 이력이었다
        df["snap"] = 0
    df["snap"] = df["snap"].fillna(0).astype(int)
    return df


def update_cache(rics: list[str], full: bool = False, today: datetime.date | None = None) -> tuple[pd.DataFrame, list]:
    """캐시를 이번 달까지 채운다 — (캐시, 받기 실패 종목). 월말 이력이 있는 종목은 마지막 월말부터(정정 반영),
    없는 종목(처음이거나 지난번 실패해 스냅샷 행만 있는 종목)은 START 부터. 시작 달이 같은 종목끼리 묶어 받는다.
    이번 달 행은 지금 값(snap=1)으로 덮는다."""
    today = today or datetime.date.today()
    cur = today.strftime("%Y-%m")
    old = pd.DataFrame(columns=COLS) if full else _load_cache()
    old = old[old["month"] < cur]
    hist = old[old["snap"] != 1]
    last = hist.groupby("ric")["month"].max().to_dict()
    groups = {}                                   # 시작 달 → 종목
    for r in rics:
        groups.setdefault(last.get(r, START[:7]), []).append(r)
    rows, skipped = [], []
    for since, rs in sorted(groups.items()):
        print(f"{'처음(또는 이력 없던) 종목' if since == START[:7] else '있던 종목'} {len(rs)}개 — {since}부터")
        rows += [(*x, 0) for x in _fetch_hist(rs, since + "-01", today.isoformat(), skipped)]
    snap = F._rows(rics, NOW_FIELDS)
    rows += [(ric, cur, _num(v[0]), _num(v[1]), _num(v[2]), 1) for ric, v in snap.items()]
    add = pd.DataFrame(rows, columns=COLS)
    df = pd.concat([old, add]).drop_duplicates(["ric", "month"], keep="last")
    df = df.sort_values(["ric", "month"]).reset_index(drop=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, CACHE)
    return df, skipped


def build(save: bool = True, full: bool = False) -> dict:
    warnings.filterwarnings("ignore")
    if not R.available(force=True):
        raise RuntimeError(f"Refinitiv 미연결 — {R.status()['error']}")
    today = datetime.date.today()
    series, plan = F.series_plan()
    heads = F.etfs([c for k, c, _ in plan.values() if k == "etf"])
    picks = {}                                    # key → ([(ric, w)…], 덮은 비중 %)
    for s in series:
        kind, code, _ = plan[s["key"]]
        picks[s["key"]] = ([(R.ric_for(code), 100.0)], 100.0) if kind == "stock" else pick(heads.get(code, {}).get("holdings") or [])
    rics = {c for p, _ in picks.values() for c, _ in p}
    rics = sorted(rics | {F.PREF[c] for c in rics if c in F.PREF})   # 우선주 환산에 보통주 값도
    print(f"고유 종목 {len(rics)}개")
    df, skipped = update_cache(rics, full=full, today=today)

    cur = today.strftime("%Y-%m")
    dates = months(START, today)
    tab = {m: {} for m in dates}
    for r in df.itertuples(index=False):
        if r.month in tab:
            tab[r.month][r.ric] = (_num(r.price), _num(r.pe), _num(r.pb))
    tab = {m: clean_row(row) for m, row in tab.items()}
    now_price = {c: v[0] for c, v in tab[cur].items()}
    items = {}
    for s in series:
        key = s["key"]
        p, cov = picks[key]
        pe, pb, pe_cov, pb_cov = zip(*(basket_month(p, tab[m], now_price) for m in dates)) if p else ([None] * len(dates),) * 4
        note = None
        if not p:
            note = "구성 종목 없음"
        elif key in F.NO_PER:
            pe, note = [None] * len(dates), F.NO_PER[key]
        elif plan[key][0] == "etf":
            note = plan[key][2] or None
        elif p[0][0] in F.PREF:
            note = F.PREF_NOTE
        items[key] = {"dates": dates, "pe": [F._r(x) for x in pe], "pb": [F._r(x) for x in pb],
                      "pe_cov": [None if x is None else round(x) for x in pe_cov],
                      "pb_cov": [None if x is None else round(x) for x in pb_cov],
                      "coverage_pct": round(cov, 1), "n": len(p), "note": note}
    d = {"generated_at": datetime.datetime.now().isoformat(timespec="seconds"), "method": METHOD,
         "partial": bool(skipped), "skipped": sorted(set(skipped)), "items": items}
    if skipped:
        print(f"⚠ 일부만 받음 — 월말 이력 받기 실패 {len(set(skipped))}종목(다음 실행 때 다시): {', '.join(sorted(set(skipped))[:20])}")
    if save:
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, OUT)
    return d


def load() -> dict | None:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def age_days() -> float | None:
    d = load()
    if not d:
        return None
    return (datetime.datetime.now() - datetime.datetime.fromisoformat(d["generated_at"])).total_seconds() / 86400


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = time.time()
    d = build(full="--full" in sys.argv)
    for k, x in d["items"].items():
        last = next((v for v in reversed(x["pe"]) if v is not None), None)
        print(f"{k:<14} 종목 {x['n']:>3} · 덮은 비중 {x['coverage_pct']:>5}% · 최근 PER {last}")
    print(f"{time.time() - t0:.0f}초 · {OUT}")
