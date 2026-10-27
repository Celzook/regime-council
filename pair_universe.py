# -*- coding: utf-8 -*-
"""페어트레이딩 종목 마스터·후보·가격 이력 — pairs.py 가 읽는다. Refinitiv·DART 호출은 이 파일과 dart_src.py 에서만 한다.

원천(2026-10-27 실측):
  종목 마스터  Refinitiv SCREEN(U(IN(Equity(active,public))),IN(TR.ExchangeCountryCode,"KR")) — KOSPI(.KS)·KOSDAQ(.KQ)
              보통주(Ordinary Shares)·우선주(Preference Shares)만. 코넥스(.KN)·ETF·리츠 아닌 펀드는 뺀다.
              TR.OrganizationID 가 같으면 같은 법인(보통·우선 짝), TR.GICSIndustryCode(6자리) = GICS Industry,
              TR.CompanyMarketCap = 법인 시가총액(보통·우선 같은 값 — 순위는 보통주로만 센다).
  국문 이름   KRX OPEN API 일별 매매정보(stk_bydd_trd·ksq_bydd_trd)의 ISU_NM — 표시용 이름만(값은 쓰지 않는다). 없으면 영문 이름.
  지분       dart_src.links — Refinitiv 에 주주 정보가 없어 예외 원천(이 계정 TR.InvestorFullName 빈 값).
  가격       Refinitiv TR.TotalReturn(일간 %, 배당 재투자) · TR.PriceClose · TR.Volume 을 한 번에(같은 날짜 행으로 온다).
              거래대금 = 종가 × 거래량(원) — TR.TurnoverValue 는 이 계정에서 안 옴.
              data/history/pairs_ret_daily.csv(수익률 %), pairs_tv_daily.csv(거래대금 원). 증분 — 최근 REFETCH_DAYS 를 다시 받아 덮어쓴다.
              수익률은 일간 값이라 다시 받아도 과거 레벨이 바뀌지 않는다(총수익 가격 splice 가 필요 없다).
  지수 편입 근사  idx_proxy = KOSPI 보통주 시총 200위·KOSDAQ 보통주 150위 안(현재 시총). 2021-05~2023-11 부분 공매도 허용기
              (KOSPI200·KOSDAQ150 만)의 편입 여부를 과거 구성 종목 대신 쓴다 — Refinitiv 0#.KS200 체인·TR.IndexConstituentRIC 가
              이 계정에서 안 열림(2026-10-27). 근사라 화면에 표시한다.
캐시: data/pairs_master.json(MASTER_MAX_AGE_DAYS 안이면 그대로, Refinitiv 값이라 저장소 제외).
"""
import datetime as dt
import json
import os
import time

import pandas as pd
import requests

import config
import dart_src
import refinitiv_src as R

MASTER = config.DATA_DIR / "pairs_master.json"
RET = config.DATA_DIR / "history" / "pairs_ret_daily.csv"
TV = config.DATA_DIR / "history" / "pairs_tv_daily.csv"
CATALOG = config.BASE_DIR / "universe" / "pairs.json"
SCREEN = 'SCREEN(U(IN(Equity(active,public))),IN(TR.ExchangeCountryCode,"KR"))'
FIELDS = ["TR.CommonName", "TR.InstrumentType", "TR.GICSIndustryCode", "TR.GICSIndustry", "TR.CompanyMarketCap", "TR.OrganizationID",
          "TR.SharesIssued"]
SHARE_TOL = (0.25, 3.0)   # DART 지분율과 '보유 주식 수 ÷ 발행주식 수' 차이 허용 — 상대 25% 또는 3%p 중 큰 쪽(자사주·결산일 뒤 증자 차이)
SPLIT_FACTORS = (2, 4, 5, 10)   # 결산일 뒤 액면분할·주식병합이면 비율이 이 배수(또는 역수)로 어긋난다 — 2026-10-27 실측 ×10·×5·×0.5
SPLIT_TOL = 0.02
KINDS = {"Ordinary Shares": "보통", "Preference Shares": "우선"}
MARKETS = {"KS": "KOSPI", "KQ": "KOSDAQ"}
IDX_TOP = {"KOSPI": 200, "KOSDAQ": 150}
SPAC = ("스팩", "SPAC", "Acquisition", "기업인수목적")
MASTER_MAX_AGE_DAYS = 7
REFETCH_DAYS = 10
BATCH = 25              # get_data 한 번에 RIC 수 — 4필드 × 5년 구간이라 panel 기본(50)의 절반
KRX_URL = "https://data-dbg.krx.co.kr/svc/apis/sto/{}"
KIND_ORDER = {"보통·우선": 0, "모자회사": 1, "동종업종": 2}


def _cat() -> dict:
    try:
        return json.loads(CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _param(name, default):
    """카탈로그 값 — 최상위나 params 안 어느 쪽이든. 값이 {"value": …} 꼴이면 value."""
    c = _cat()
    v = c.get(name, (c.get("params") or {}).get(name, default))
    return v.get("value", default) if isinstance(v, dict) else v


# ── 국문 이름(KRX, 표시용) ──
def krx_names(today: dt.date | None = None) -> dict:
    """{종목코드: 국문 약명} — 가장 최근 영업일의 KRX 일별 매매정보. 실패하면 {}(영문 이름으로 대신)."""
    try:
        key = config.api_key("KRX_API_KEY")
    except RuntimeError:
        return {}
    today = today or dt.date.today()
    for back in range(1, 10):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        out = {}
        for svc in ("stk_bydd_trd", "ksq_bydd_trd"):
            try:
                r = requests.get(KRX_URL.format(svc), params={"basDd": f"{d:%Y%m%d}"}, headers={"AUTH_KEY": key}, timeout=30)
                rows = r.json().get("OutBlock_1") or []
            except (requests.RequestException, ValueError):
                rows = []
            out.update({x["ISU_CD"]: x["ISU_NM"] for x in rows if x.get("ISU_CD") and x.get("ISU_NM")})
        if out:
            return out
    return {}


# ── 종목 마스터 ──
def build_master(names: dict | None = None) -> pd.DataFrame:
    """Refinitiv 스크리닝 → 마스터 DataFrame(index = ric). 열은 pairs_spec 의 master() 와 같다."""
    ek = R._ek()
    df, err = R._call(ek.get_data, SCREEN, FIELDS)
    if df is None or not len(df):
        raise RuntimeError(f"Refinitiv 종목 스크리닝 빈 응답 — {err}")
    df = df.rename(columns={"Instrument": "ric", "Company Common Name": "name_en", "Instrument Type": "itype",
                            "GICS Industry Code": "gics_code", "GICS Industry Name": "gics_name",
                            "Company Market Cap": "mktcap", "Organization PermID": "org_id", "Issued": "shares"})
    df["suffix"] = df["ric"].str.extract(r"\.(\w+)$")[0]
    df = df[df["itype"].isin(KINDS) & df["suffix"].isin(MARKETS)].copy()
    df["code"] = df["ric"].str.split(".").str[0]
    df["kind"] = df["itype"].map(KINDS)
    df["market"] = df["suffix"].map(MARKETS)
    names = krx_names() if names is None else names
    df["name"] = [names.get(c) or n for c, n in zip(df["code"], df["name_en"])]
    df = df[~df["name"].astype(str).str.contains("|".join(SPAC)) & ~df["name_en"].astype(str).str.contains("|".join(SPAC))]
    df["mktcap"] = pd.to_numeric(df["mktcap"], errors="coerce")
    df["shares"] = pd.to_numeric(df.get("shares"), errors="coerce")
    df["gics_code"] = df["gics_code"].map(lambda v: None if pd.isna(v) else str(int(v)) if str(v).replace(".0", "").isdigit() else str(v))
    df["org_id"] = df["org_id"].map(lambda v: None if pd.isna(v) else str(v))
    df["idx_proxy"] = False
    for mkt, n in IDX_TOP.items():
        top = df[(df["market"] == mkt) & (df["kind"] == "보통")].nlargest(n, "mktcap").index
        df.loc[top, "idx_proxy"] = True
    df = df.drop_duplicates("ric").set_index("ric")
    return df[["code", "name", "name_en", "kind", "market", "gics_code", "gics_name", "mktcap", "shares", "org_id", "idx_proxy"]].sort_index()


def master(refresh: bool = False) -> pd.DataFrame:
    """캐시가 MASTER_MAX_AGE_DAYS 안이면 캐시. Refinitiv 미연결이면 옛 캐시(없으면 RuntimeError)."""
    old = None
    try:
        j = json.loads(MASTER.read_text(encoding="utf-8"))
        old = pd.DataFrame(j["rows"]).set_index("ric")
        age = (dt.datetime.now() - dt.datetime.fromisoformat(j["fetched_at"])).days
        if not refresh and age < MASTER_MAX_AGE_DAYS:
            return old
    except (OSError, ValueError, KeyError):
        pass
    if not R.available(force=True):
        if old is not None:
            print(f"  Refinitiv 미연결 — 옛 종목 마스터 사용: {R.status()['error']}")
            return old
        raise RuntimeError(f"종목 마스터 없음 — Refinitiv 미연결: {R.status()['error']}")
    m = build_master()
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    tmp = MASTER.with_suffix(".json.tmp")
    rows = m.reset_index().astype(object).where(m.reset_index().notna(), None).to_dict("records")
    tmp.write_text(json.dumps({"fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "source": "Refinitiv SCREEN · KRX 국문 이름",
                               "rows": rows}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, MASTER)
    return m


# ── 후보 ──
def verify_shares(links: list[dict], m: pd.DataFrame, tol=SHARE_TOL) -> tuple[list[dict], list[dict]]:
    """이름으로 이은 관계를 주식 수로 확인 — DART 보유 주식 수 ÷ 자회사 발행주식 수(Refinitiv TR.SharesIssued — 자사주 포함,
    DART 지분율과 같은 분모. TR.SharesOutstanding 은 자사주를 빼서 지주사 자회사가 잘못 빠졌다) 가 공시 지분율과 맞아야 남긴다.
    이름만 같은 비상장 자회사는 여기서 걸러진다(2026-10-27 실측: DS단석→'하이브' 100%, 현대자동차→'HMM' 99.99%).
    비율이 분할·병합 배수(SPLIT_FACTORS 또는 역수, ±2%)면 결산일 뒤 주식 수 변화로 보고 남긴다(share_note) — 주식 수가 늘었으면
    (분할 또는 상장·증자 희석) 지분율을 낮은 쪽(주식 수로 본 값)으로 바꾼다. 같은 배수로 우연히 맞는 이름 충돌은 남을 수 있다.
    주식 수를 모르면(어느 한쪽 없음) 확인 못 함으로 뺀다. 반환 (남긴 것, 뺀 것 + 사유)."""
    shares = {r.code: r.shares for r in m.itertuples() if r.kind == "보통"}
    keep, drop = [], []
    for l in links:
        n = shares.get(l["child"])
        if not l.get("qy") or n is None or pd.isna(n) or not n:
            drop.append({**l, "reason": "주식 수 확인 못 함"})
            continue
        implied = l["qy"] / n * 100
        ratio = implied / l["pct"] if l["pct"] else 0
        split = next((f for f in SPLIT_FACTORS for r in (f, 1 / f) if abs(ratio / r - 1) <= SPLIT_TOL), None)
        if abs(implied - l["pct"]) <= max(tol[0] * l["pct"], tol[1]):
            keep.append({**l, "implied_pct": round(implied, 2)})
        elif split and ratio > 1:     # 지금 주식 수가 공시 때의 1/k — 결산일 뒤 주식 병합(지분율은 그대로)
            keep.append({**l, "implied_pct": round(implied, 2),
                         "share_note": f"주식 수 비율 ×{ratio:.2g} — 결산일 뒤 병합으로 보고 공시 지분율 사용"})
        elif split:                   # 지금 주식 수가 k 배 — 분할(지분율 그대로) 또는 상장·증자 희석(지분율 감소). 구분할 수 없어 낮은 쪽
            keep.append({**l, "pct": round(implied, 2), "implied_pct": round(implied, 2), "dart_pct": l["pct"],
                         "share_note": f"주식 수 비율 ×{ratio:.2g} — 결산일 뒤 분할 또는 증자 희석, 낮은 쪽 {implied:.1f}% 사용"})
        else:
            drop.append({**l, "implied_pct": round(implied, 2), "reason": f"주식 수로 본 지분율 {implied:.1f}% ≠ 공시 {l['pct']}% — 이름만 같은 다른 회사이거나 결산 뒤 지분 변동(필요하면 pairs_overrides 로 확인)"})
    return keep, drop


def dart_links(m: pd.DataFrame, min_pct: float | None = None) -> dict:
    """DART 상장 모·자 관계(보통주끼리, 주식 수 확인 통과분). 실패하면 빈 links + error — 모자회사 후보만 빠지고 나머지는 계속."""
    min_pct = _param("dart_min_pct", 7.0) if min_pct is None else min_pct
    codes = set(m.loc[m["kind"] == "보통", "code"])
    try:
        d = dart_src.links(codes, min_pct=min_pct)
    except Exception as e:  # noqa: BLE001
        print(f"  경고: DART 지분 관계 실패 — 모자회사 후보 없이 계속: {e}")
        return {"links": [], "unmatched": [], "rejected": [], "error": str(e), "source": dart_src.SOURCE}
    d["links"], d["rejected"] = verify_shares(d["links"], m)
    return d


def candidates(m: pd.DataFrame, links: dict | list | None = None, top_n: int | None = None) -> list[dict]:
    """보통·우선(같은 org_id) · 모자회사(DART ≥7%) · 동종업종(GICS Industry 보통주 시총 상위 top_n 조합).
    같은 (a,b) 가 여러 종류에 걸리면 보통·우선 > 모자회사 > 동종업종 하나만, note 에 겹친 종류. overrides.exclude 는 뺀다."""
    top_n = int(_param("top_n_industry", 10)) if top_n is None else top_n
    links = (links or {}).get("links", []) if isinstance(links, dict) else (links or [])
    by_code = {r.code: ric for ric, r in m.iterrows() if r.kind == "보통"}
    out = {}

    def add(a, b, kind, pct=None, as_of=None, note=""):
        key = frozenset((a, b))
        c = {"id": f"{a}~{b}", "a": a, "b": b, "kind": kind, "pct": pct, "as_of": as_of, "note": note}
        if key in out:
            prev = out[key]
            keep, drop = (prev, c) if KIND_ORDER[prev["kind"]] <= KIND_ORDER[kind] else (c, prev)
            keep["note"] = "; ".join(x for x in (keep["note"], f"{drop['kind']}에도 해당") if x)
            if drop["kind"] == "모자회사" and keep["pct"] is None:
                keep["pct"], keep["as_of"] = drop["pct"], drop["as_of"]
            out[key] = keep
        else:
            out[key] = c

    common = m[m["kind"] == "보통"]
    org_common = {}
    for ric, r in common.iterrows():
        if r.org_id:
            org_common.setdefault(r.org_id, []).append(ric)
    for ric, r in m[m["kind"] == "우선"].iterrows():
        cs = org_common.get(r.org_id) or []
        if len(cs) == 1:
            add(ric, cs[0], "보통·우선")
    for l in links:
        a, b = by_code.get(l["child"]), by_code.get(l["parent"])
        if a and b and a != b:
            note = f"{l.get('parent_name', '')} → {l.get('child_name', '')} {l['pct']}%" + (f" ({l['share_note']})" if l.get("share_note") else "")
            add(a, b, "모자회사", pct=l["pct"], as_of=l.get("as_of"), note=note)
    for g, grp in common.dropna(subset=["gics_code"]).groupby("gics_code"):
        top = sorted(grp.nlargest(top_n, "mktcap").index)
        for i, a in enumerate(top):
            for b in top[i + 1:]:
                add(a, b, "동종업종", note=str(grp.loc[a, "gics_name"] or ""))
    excl = {e if isinstance(e, str) else e.get("id") for e in dart_src.load_overrides()["exclude"]}
    return sorted((c for c in out.values() if c["id"] not in excl and f"{c['b']}~{c['a']}" not in excl),
                  key=lambda c: (KIND_ORDER[c["kind"]], c["id"]))


# ── 가격 이력 ──
def _fetch(rics: list[str], start: dt.date, end: dt.date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(수익률 %, 거래대금 원) 넓은 표. 4필드를 한 번에 받는다 — 같은 행이 같은 날짜."""
    ek = R._ek()
    fields = ["TR.TotalReturn.date", "TR.TotalReturn", "TR.PriceClose", "TR.Volume"]
    rets, tvs = {}, {}
    for i in range(0, len(rics), BATCH):
        group = rics[i:i + BATCH]
        for a, b in R._chunks(start, end):
            d, _ = R._call(ek.get_data, group, fields, {"Frq": "D", "SDate": a.isoformat(), "EDate": b.isoformat()})
            if d is None or not len(d):
                continue
            d = d.copy()
            d["_d"] = pd.to_datetime(d.iloc[:, 1], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
            d["_r"] = pd.to_numeric(d.iloc[:, 2], errors="coerce")
            d["_tv"] = pd.to_numeric(d.iloc[:, 3], errors="coerce") * pd.to_numeric(d.iloc[:, 4], errors="coerce")
            for ric, g in d.dropna(subset=["_d"]).groupby("Instrument"):
                g = g.drop_duplicates("_d", keep="last").set_index("_d")
                rets.setdefault(ric, []).append(g["_r"])
                tvs.setdefault(ric, []).append(g["_tv"])
        print(f"  가격 {min(i + BATCH, len(rics))}/{len(rics)}")

    def wide(parts):
        cols = {}
        for ric, ps in parts.items():
            s = pd.concat(ps)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            cols[ric] = R._today_cut(s, ric)
        return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()
    r, t = wide(rets), wide(tvs)
    for x in (r, t):   # 주말에 직전 값이 다시 오는 경우 — 평일만
        if len(x):
            x.drop(x.index[x.index.dayofweek >= 5], inplace=True)
    return r, t


def _merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if old is None or not len(old):
        return new
    if not len(new):
        return old
    out = old.combine_first(new)
    out.update(new)          # 겹치는 날은 새 값(정정 반영)
    return out.sort_index()


def _read_csv(p) -> pd.DataFrame:
    return pd.read_csv(p, index_col=0, parse_dates=True) if p.exists() else pd.DataFrame()


def _write_csv(df: pd.DataFrame, p) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    df.index.name = "date"
    tmp = p.with_suffix(".csv.tmp")
    df.to_csv(tmp, encoding="utf-8")
    os.replace(tmp, p)


def load_prices(rics: list[str], refresh: bool = True, full: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(ret %, tv 원) — 요청한 ric 열만. refresh 면 처음 보는 열은 history_start 부터, 이미 있는 열은 최근 REFETCH_DAYS 부터 다시 받는다.
    Refinitiv 미연결이면 저장된 이력 그대로."""
    ret, tv = _read_csv(RET), _read_csv(TV)
    rics = sorted(set(rics))
    if refresh and R.available(force=True):
        start = dt.date.fromisoformat(str(_param("history_start", "2015-01-02")))
        today = dt.date.today()
        have = [] if full else [c for c in rics if c in ret.columns and ret[c].notna().any()]
        new = [c for c in rics if c not in have]
        jobs = []
        if new:
            jobs.append((new, start))
        if have:   # 마지막 날짜가 오늘이어도 다시 받는다 — 장중에 받은 값·정정 반영
            last = ret[have].index.max().date()
            since = last - dt.timedelta(days=REFETCH_DAYS)
            lv = {c: ret[c].last_valid_index().date() for c in have}
            behind = [c for c in have if lv[c] < since]      # 받기 실패·정지 해제로 끝이 비어 있는 열 — 그 열의 마지막 값부터 다시
            cur = [c for c in have if c not in behind]
            if cur:
                jobs.append((cur, max(start, since)))
            if behind:
                jobs.append((behind, max(start, min(lv[c] for c in behind) - dt.timedelta(days=REFETCH_DAYS))))
        t0 = time.time()
        for group, since in jobs:
            print(f"  가격 받기: {len(group)}종목 {since}~")
            r, t = _fetch(group, since, today)
            empty = [c for c in group if c not in r.columns or not r[c].notna().any()]
            if empty:
                print(f"  경고: 빈 응답 {len(empty)}종목(거래정지·받기 실패) — {', '.join(empty[:10])}{' …' if len(empty) > 10 else ''}")
            ret, tv = _merge(ret, r), _merge(tv, t)
        if jobs:
            _write_csv(ret, RET)
            _write_csv(tv, TV)
            print(f"  가격 저장 {RET.name}·{TV.name} ({time.time() - t0:.0f}초)")
    elif refresh:
        print(f"  Refinitiv 미연결 — 페어 가격은 저장된 이력: {R.status()['error']}")
    cols = [c for c in rics if c in ret.columns]
    return ret.reindex(columns=cols), tv.reindex(columns=[c for c in rics if c in tv.columns])


if __name__ == "__main__":
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    m = master(refresh="--master" in sys.argv)
    print(f"마스터 {len(m)}종목 (보통 {int((m.kind == '보통').sum())} · 우선 {int((m.kind == '우선').sum())}) · 지수 근사 {int(m.idx_proxy.sum())}")
    links = dart_links(m)
    cands = candidates(m, links)
    by = pd.Series([c["kind"] for c in cands]).value_counts().to_dict()
    print(f"후보 {len(cands)}쌍 {by} · DART 상장 모·자 {len(links.get('links', []))}건")
    if "--prices" in sys.argv:
        rics = sorted({x for c in cands for x in (c["a"], c["b"])})
        r, t = load_prices(rics, full="--full" in sys.argv)
        print(f"가격 {r.shape} · {r.index.min()}~{r.index.max()}")
