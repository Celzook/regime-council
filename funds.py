# -*- coding: utf-8 -*-
"""위탁운용사(펀드) 화면 — 예시 공모펀드의 수익률·BM 대비 초과수익·순자산/설정원본 증감·보수·변동성·최대낙폭.

원천: 금융투자협회 전자공시(dis.kofia.or.kr) XML 서비스(FS-DIS2). POST /proframeWeb/XMLSERVICES/
  기준가 이력 : DISFundStdPrcStutSO  펀드 1개/요청, tmpV30~31 기간, tmpV10=0 일간, tmpV12 표준코드
                → V1 기준일, V2 기준가(분배 전 원값), V4 과표기준가, V5 설정원본(백만원). V3·V6~ 은 쓰지 않는다
                  (V3 은 기준가 차와 맞지 않고, V6 은 전 영업일 KOSPI 같은 시장 참고값).
  결산·분배   : DISFundRdmpSO        기간 필터만 동작(전 펀드가 와서 1년 창 ≈ 48MB) — 대상 코드 행만 캐시에 남긴다.
                → V4 회계기간 종료일, V7 결산전 기준가, V9 결산후 기준가(상환이면 0), V10 '결산'/'상환', V11 코드
  보수        : DISFundFeeCmsSO      tmpV30 기준일 하나 → 전 펀드(≈ 2만 개, 30MB). 운용사 V1, 표준코드 V15,
                운용보수 V5, 판매 V6, 수탁 V7, 사무관리 V8, 총보수 V9, 기타비용 V11, 매매·중개수수료 V16 (%/년).
                모든 월말이 공시 기준일은 아니라 최근 월말부터 차례로 시도해 행이 오는 첫 날을 쓴다.
  (요청 형식·수정기준가 계산은 public_KPI/tools/kofia_tdf/kofia_tdf.py — 인포맥스 수정기준가와 대조 끝난 코드를 옮겼다.)

계산:
  수정기준가 = 기준가 × Π(결산전/결산후) — 그날보다 앞선 결산분만 곱한다. 순자산(억원) = 설정원본(백만원) × 기준가 / 1e5.
  수익률(1W·1M·3M·YTD·1Y) = 수정기준가 끝 ÷ 기준 − 1. 기준 = 끝 날짜에서 7일·1·3개월·전년 말·1년 뺀 날 이전의 마지막 기준일(달력 기준).
  BM 수익률은 같은 두 기준일로 잰다. BM 값은 bm.lag=n 이면 기준일 D 보다 앞선 n 번째 거래일 값(0 이면 D 당일까지).
    국내 주식 펀드 기준가 D 는 D-1 장 마감 가격이다 — 2026-10-26 실측(최근 1년 일간 로그 변화 상관): 교보파워인덱스 vs KOSPI200
    lag0 −0.11 · lag1 1.00, 한국밸류10년 vs KOSPI lag1 0.99. 미국 주식 펀드(KB S&P500 H)는 lag2 0.96(lag0·1·3 ≈ 0).
  초과수익 = 펀드 − BM (%p). 변동성 = 최근 1년 일간 로그수익률 표준편차 × √252. 최대낙폭 = 고점 대비 최저(1년·HIST_START 이후).
  순자산·설정원본 증감 = 끝 ÷ 1·3개월 전 기준 − 1(%), 차액(억원).
  차트 = 수정기준가·BM 주간(금요일 기준 마지막 값), 최근 CHART_YEARS 년, 첫 공통 날 = 100.
캐시(data/_raw/kofia/, 저장소 제외): nav_<코드>.json(증분 — 마지막 날 − REFETCH_DAYS 부터 다시), rdmp.json(대상 코드 결산만),
  fee_<기준일>.xml.gz(원 응답). 출력: data/funds.json. 20시간 안에 만든 결과가 있으면 run() 은 건너뛴다(force=True 면 다시).
사용법: python -X utf8 funds.py [--force]
"""
import datetime as dt
import gzip
import html
import json
import math
import os
import re
import sys
import time

import numpy as np
import pandas as pd
import requests

import config
import universe

OUT = config.DATA_DIR / "funds.json"
CATALOG = universe.DIR / "funds.json"
CACHE = config.DATA_DIR / "_raw" / "kofia"
HIST_DIR = config.DATA_DIR / "history"
URL = "https://dis.kofia.or.kr/proframeWeb/XMLSERVICES/"
HEADERS = {"Content-Type": "application/xml; charset=UTF-8", "User-Agent": "Mozilla/5.0",
           "Referer": "https://dis.kofia.or.kr/websquare/index.jsp"}
SOURCE = "금융투자협회 전자공시(dis.kofia.or.kr)"
HIST_START = dt.date(2016, 1, 1)   # 이력 시작(결산 서비스가 전 펀드를 줘서 무겁다 — 10년으로 제한). '전체 기간' 최대낙폭은 이 날부터
STALE_HOURS = 20
REFETCH_DAYS = 10                  # 증분 때 마지막 날짜보다 이만큼 앞부터 다시 받는다(정정 반영)
RDMP_REFETCH_DAYS = 60             # 늦게 올라오는 결산을 흡수
RDMP_WINDOW_DAYS = 365
FEE_MAX_AGE_DAYS = 35
CHART_YEARS = 3
DELAY = 1.0                        # 요청 사이 간격(초) — 협회 서버 예의
PERIODS = ("1W", "1M", "3M", "YTD", "1Y")
METHOD = ("수정기준가(분배 반영) 기준, 달력 기준 기간(직전 기준일). BM 은 같은 두 기준일의 값(기준가 D 는 앞선 장 마감 가격이라 BM 은 국내 D-1·미국 D-2 거래일)."
          " 초과수익 = 펀드 − BM(%p). 변동성 = 1년 일간 로그수익률 표준편차×√252. 최대낙폭 = 고점 대비 최저.")


# ── 금투협 호출·파싱 ──
def _body(svc: str, cond: dict) -> bytes:
    c = "".join(f"<{k}>{v}</{k}>" for k, v in cond.items())
    return ('<?xml version="1.0" encoding="utf-8"?><message><proframeHeader><pfmAppName>FS-DIS2</pfmAppName>'
            f'<pfmSvcName>{svc}</pfmSvcName><pfmFnName>select</pfmFnName></proframeHeader><systemHeader></systemHeader>'
            f'<DISCondFuncDTO>{c}</DISCondFuncDTO></message>').encode("utf-8")


def parse(text: str) -> list[dict]:
    """XML 응답 → selectMeta 행 목록({'tmpV1': ..}). 값의 &amp; 등은 풀어 둔다."""
    return [{k: html.unescape(v) for k, v in re.findall(r"<(tmpV\d+)>([^<]*)</tmpV\d+>", m)}
            for m in re.findall(r"<selectMeta>(.*?)</selectMeta>", text, re.S)]


def _post(svc: str, cond: dict, timeout: int = 300) -> str:
    """웹방화벽이 가끔 연결을 끊어 재시도한다."""
    for i in range(5):
        try:
            r = requests.post(URL, data=_body(svc, cond), headers=HEADERS, timeout=(30, timeout))
            r.raise_for_status()
            break
        except requests.RequestException:
            if i == 4:
                raise
            time.sleep(3 * (i + 1))
    time.sleep(DELAY)
    return r.text


def _ymd(d: dt.date) -> str:
    return f"{d:%Y%m%d}"


def _iso(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _num(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return float("nan")


def _load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(p, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def nav_rows(rows: list[dict]) -> dict[str, dict]:
    """기준가 서비스 행 → {YYYY-MM-DD: {date, nav, units_mn}}. 기준가가 0·빈 값인 행은 뺀다."""
    out = {}
    for d in rows:
        nav = _num(d.get("tmpV2"))
        if d.get("tmpV1") and nav > 0:
            day = _iso(d["tmpV1"])
            u = _num(d.get("tmpV5"))
            out[day] = {"date": day, "nav": nav, "units_mn": 0.0 if math.isnan(u) else u}
    return out


def history(code: str, since: dt.date, until: dt.date) -> list[dict]:
    """기준가 이력(오름차순). 캐시가 since 부터 받은 것이면 마지막 날짜 − REFETCH_DAYS 부터만 다시 받아 합친다."""
    p = CACHE / f"nav_{code}.json"
    st = _load(p, {"since": None, "rows": []})
    old = {r["date"]: r for r in st["rows"]}
    start = since
    if old and st["since"] and st["since"] <= since.isoformat():
        start = max(since, dt.date.fromisoformat(max(old)) - dt.timedelta(days=REFETCH_DAYS))
    got = nav_rows(parse(_post("DISFundStdPrcStutSO", dict(tmpV30=_ymd(start), tmpV31=_ymd(until), tmpV10="0", tmpV12=code))))
    if not got and not old:
        raise RuntimeError(f"기준가 {code} {start}~{until} 0건 — 표준코드 확인 또는 일시 차단")
    old.update(got)
    rows = sorted(old.values(), key=lambda r: r["date"])
    _save(p, {"since": min(since.isoformat(), st["since"] or "9999"), "rows": rows})
    return rows


def settle_rows(rows: list[dict], codes: set[str]) -> list[dict]:
    """결산 서비스 행 → 대상 코드의 [{code, end, kind, before, after}]."""
    out = []
    for d in rows:
        if d.get("tmpV11") in codes and d.get("tmpV4"):
            out.append({"code": d["tmpV11"], "end": _iso(d["tmpV4"]), "kind": d.get("tmpV10", ""),
                        "before": _num(d.get("tmpV7")), "after": _num(d.get("tmpV9"))})
    return out


def settlements(codes: set[str], since: dt.date, until: dt.date) -> dict[str, list[tuple[str, float, float]]]:
    """코드별 [(회계기간 종료일, 결산전, 결산후)]. 받은 구간·코드는 rdmp.json 에 남겨 다시 받지 않는다.
    대상 코드가 새로 생기면 처음부터 다시 받는다(과거 결산 누락 방지)."""
    p = CACHE / "rdmp.json"
    st = _load(p, {"from": None, "to": None, "codes": [], "rows": []})
    full = st["from"] is None or st["from"] > since.isoformat() or not codes <= set(st["codes"])
    rows = {} if full else {(r["code"], r["end"]): r for r in st["rows"]}
    s = since if full else max(since, dt.date.fromisoformat(st["to"]) - dt.timedelta(days=RDMP_REFETCH_DAYS))
    while s <= until:
        e = min(until, s + dt.timedelta(days=RDMP_WINDOW_DAYS - 1))
        got = parse(_post("DISFundRdmpSO", dict(tmpV30=_ymd(s), tmpV31=_ymd(e)), timeout=600))
        if not got and (e - s).days >= 30:   # 한 달 넘는 창에 전 펀드 결산이 0건이면 차단·오류로 본다
            raise RuntimeError(f"결산 {s}~{e} 0건 — 일시 차단 의심, 캐시는 갱신하지 않음")
        rows.update({(r["code"], r["end"]): r for r in settle_rows(got, codes)})
        s = e + dt.timedelta(days=1)
    st = {"from": since.isoformat() if full else st["from"], "to": until.isoformat(),
          "codes": sorted(codes if full else codes | set(st["codes"])),
          "rows": sorted(rows.values(), key=lambda r: (r["code"], r["end"]))}
    _save(p, st)
    out: dict[str, list] = {c: [] for c in codes}
    for r in st["rows"]:
        if r["code"] in out and r["after"] > 0 and r["before"] > 0:   # after=0: 상환이거나 휴면 중 빈 결산
            out[r["code"]].append((r["end"], r["before"], r["after"]))
    return out


def adjust(hist: list[dict], sett: list[tuple[str, float, float]]) -> list[dict]:
    """수정기준가·순자산(억원)을 붙인다. 결산 종료일 다음 날부터 그 결산의 계수(결산전/결산후)를 곱한다."""
    ev = sorted((e, b / a) for e, b, a in sett)
    return [{**r, "adj_nav": r["nav"] * math.prod(x for e, x in ev if e < r["date"]),
             "aum_eok": r["units_mn"] * r["nav"] / 1e5} for r in hist]


def fee_rows(rows: list[dict], codes: set[str]) -> dict[str, dict]:
    """보수 공시 행 → {표준코드: 보수(%/년)}."""
    out = {}
    for d in rows:
        c = d.get("tmpV15")
        if c in codes:
            f = {k: _num(d.get(v)) for k, v in (("mgmt", "tmpV5"), ("sales", "tmpV6"), ("trustee", "tmpV7"), ("admin", "tmpV8"),
                                                ("total", "tmpV9"), ("other", "tmpV11"), ("trading", "tmpV16"))}
            f["ter"] = f["total"] + np.nan_to_num(f["other"]) + np.nan_to_num(f["trading"])   # 실부담 = 총보수 + 기타비용 + 매매·중개
            out[c] = {"manager": d.get("tmpV1", ""), "name": d.get("tmpV2", ""), "setup": _iso(d["tmpV4"]) if d.get("tmpV4") else None,
                      **{k: (None if math.isnan(v) else round(float(v), 4)) for k, v in f.items()}}
    return out


def _month_ends(today: dt.date, n: int) -> list[dt.date]:
    out, d = [], today.replace(day=1) - dt.timedelta(days=1)
    for _ in range(n):
        out.append(d)
        d = d.replace(day=1) - dt.timedelta(days=1)
    return out


def fees(codes: set[str], today: dt.date) -> tuple[str | None, dict[str, dict]]:
    """가장 최근 보수 공시 기준일과 대상 코드의 보수. 캐시가 FEE_MAX_AGE_DAYS 안이면 다시 받지 않는다."""
    cached = sorted(CACHE.glob("fee_*.xml.gz"))
    if cached:
        d = cached[-1].name[4:12]
        if (today - dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))).days <= FEE_MAX_AGE_DAYS:
            with gzip.open(cached[-1], "rt", encoding="utf-8") as f:
                return _iso(d), fee_rows(parse(f.read()), codes)
    for me in _month_ends(today, 4):
        p = CACHE / f"fee_{_ymd(me)}.xml.gz"
        if p.exists():
            text = gzip.open(p, "rt", encoding="utf-8").read()
        else:
            text = _post("DISFundFeeCmsSO", dict(tmpV30=_ymd(me)), timeout=600)
            if "<selectMeta>" not in text:   # 공시 기준일이 아닌 월말 — 0건
                continue
            CACHE.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, "wt", encoding="utf-8") as f:
                f.write(text)
        return me.isoformat(), fee_rows(parse(text), codes)
    if cached:   # 새 기준일을 못 찾으면 묵은 캐시라도
        d = cached[-1].name[4:12]
        with gzip.open(cached[-1], "rt", encoding="utf-8") as f:
            return _iso(d), fee_rows(parse(f.read()), codes)
    return None, {}


# ── BM ──
def bm_series(spec: dict, hist_dir=None) -> pd.Series:
    """BM 수준 시계열(날짜 오름차순). yield_accrual: 연 % 금리를 다음 관측일까지의 달력 일수만큼 쌓은 지수(시작 100)."""
    df = pd.read_csv((hist_dir or HIST_DIR) / spec["file"], index_col=0, parse_dates=True, usecols=["date", spec["col"]])
    s = pd.to_numeric(df[spec["col"]], errors="coerce").dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if spec.get("kind", "price") == "yield_accrual":
        days = s.index.to_series().diff().dt.days.fillna(0).to_numpy()
        growth = 1 + s.shift(1).fillna(0).to_numpy() / 100 * days / 365
        s = pd.Series(100 * np.cumprod(growth), index=s.index)
    return s[s > 0]


def align_bm(dates: pd.DatetimeIndex, bm: pd.Series, lag: int) -> pd.Series:
    """펀드 기준일마다 BM 값 — lag=0 이면 그날까지의 마지막 거래일, lag=n(≥1) 이면 그날보다 앞선 n 번째 거래일."""
    pos = bm.index.searchsorted(dates, side="left" if lag else "right") - max(lag, 1)
    vals = np.where(pos >= 0, bm.to_numpy()[np.clip(pos, 0, None)], np.nan)
    return pd.Series(vals, index=dates)


# ── 지표(순수 함수) ──
def base_date(idx: pd.DatetimeIndex, end: pd.Timestamp, period: str) -> pd.Timestamp | None:
    """기간 시작 기준일 = 목표일 이전(포함)의 마지막 기준일. 이력이 목표일보다 늦게 시작하면 None."""
    target = {"1W": end - pd.Timedelta(days=7), "1M": end - pd.DateOffset(months=1), "3M": end - pd.DateOffset(months=3),
              "YTD": pd.Timestamp(end.year - 1, 12, 31), "1Y": end - pd.DateOffset(years=1)}[period]
    pos = idx.searchsorted(target, side="right") - 1
    return idx[pos] if pos >= 0 else None


def _pct(a, b) -> float | None:
    if a is None or b is None or not np.isfinite(a) or not np.isfinite(b) or b == 0:
        return None
    return round((a / b - 1) * 100, 2)


def max_drawdown(s: pd.Series) -> float | None:
    s = s.dropna()
    if len(s) < 2:
        return None
    return round(float((s / s.cummax() - 1).min() * 100), 2)


def vol(s: pd.Series) -> float | None:
    r = np.log(s.dropna()).diff().dropna()
    if len(r) < 20:
        return None
    return round(float(r.std(ddof=1) * math.sqrt(252) * 100), 2)


def weekly(nav: pd.Series, bm: pd.Series, years: int = CHART_YEARS) -> dict:
    """차트용 주간(금요일 마감 기준 마지막 값) 누적 — 첫 공통 날 = 100."""
    df = pd.DataFrame({"fund": nav, "bm": bm}).dropna()
    df = df[df.index > df.index[-1] - pd.DateOffset(years=years)] if len(df) else df
    w = df.groupby(df.index.to_period("W-FRI")).tail(1)   # 주마다 실제 마지막 기준일 — 마지막 점은 최신일
    if not len(w):
        return {"dates": [], "fund": [], "bm": []}
    w = w / w.iloc[0] * 100
    return {"dates": [d.strftime("%Y-%m-%d") for d in w.index], "fund": [round(float(x), 2) for x in w["fund"]],
            "bm": [round(float(x), 2) for x in w["bm"]]}


def _bm_date(bm: pd.Series | None, end: pd.Timestamp, lag: int) -> str | None:
    if bm is None or not len(bm):
        return None
    pos = bm.index.searchsorted(end, side="left" if lag else "right") - max(lag, 1)
    return bm.index[pos].strftime("%Y-%m-%d") if pos >= 0 else None


def fund_metrics(rows: list[dict], bm: pd.Series | None, lag: int = 1) -> dict:
    """adjust() 결과 행(오름차순)과 BM 수준 시계열 → 화면 지표. 네트워크·파일 없음."""
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["date"])
    nav, aum, units = df["adj_nav"].astype(float), df["aum_eok"].astype(float), df["units_mn"].astype(float) / 100   # 설정원본 억원
    end = nav.index[-1]
    b = align_bm(nav.index, bm, lag) if bm is not None and len(bm) else pd.Series(np.nan, index=nav.index)
    ret, bret, exc = {}, {}, {}
    for p in PERIODS:
        d0 = base_date(nav.index, end, p)
        ret[p] = _pct(nav[end], nav[d0]) if d0 is not None else None
        bret[p] = _pct(b[end], b[d0]) if d0 is not None else None
        exc[p] = round(ret[p] - bret[p], 2) if ret[p] is not None and bret[p] is not None else None
    flows = {}
    for p in ("1M", "3M"):
        d0 = base_date(nav.index, end, p)
        flows[f"aum_chg_{p.lower()}_pct"] = _pct(aum[end], aum[d0]) if d0 is not None else None
        flows[f"aum_chg_{p.lower()}_eok"] = round(float(aum[end] - aum[d0]), 1) if d0 is not None else None
        flows[f"units_chg_{p.lower()}_pct"] = _pct(units[end], units[d0]) if d0 is not None else None
        flows[f"units_chg_{p.lower()}_eok"] = round(float(units[end] - units[d0]), 1) if d0 is not None else None
    y1 = nav[nav.index > end - pd.DateOffset(years=1)]
    by1 = b[b.index > end - pd.DateOffset(years=1)]
    last = df.iloc[-1]
    return {
        "nav_date": end.strftime("%Y-%m-%d"), "nav": round(float(last["nav"]), 2), "adj_nav": round(float(last["adj_nav"]), 2),
        "aum_eok": round(float(last["aum_eok"]), 1), "units_eok": round(float(units[end]), 1),
        "returns": ret, "bm_returns": bret, "excess": exc, "flows": flows,
        "risk": {"vol_1y": vol(y1), "bm_vol_1y": vol(by1), "mdd_1y": max_drawdown(y1), "bm_mdd_1y": max_drawdown(by1),
                 "mdd_all": max_drawdown(nav), "mdd_all_from": nav.index[0].strftime("%Y-%m-%d")},
        "bm_date": _bm_date(bm, end, lag),
        "chart": weekly(nav, b),
        "history_rows": len(df),
    }


# ── 조립 ──
def load_catalog(path=None) -> list[dict]:
    return json.loads((path or CATALOG).read_text(encoding="utf-8"))["funds"]


def _clean_err(e: Exception) -> str:
    return re.sub(r"[A-Za-z]:\\[^\s'\"]+|/(?:home|Users|mnt|tmp)/[^\s'\"]+", lambda m: re.split(r"[\\/]+", m.group(0))[-1], f"{type(e).__name__}: {e}")


def build(save: bool = True, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    cat = load_catalog()
    codes = {f["code"] for f in cat}
    errors: list[str] = []
    try:
        sett = settlements(codes, HIST_START, today)
    except Exception as e:  # noqa: BLE001 — 결산을 못 받으면 분배 미반영으로라도 보여 준다
        errors.append("결산(분배) 조회 실패 — 수정기준가에 분배 미반영: " + _clean_err(e))
        sett = {c: [] for c in codes}
    try:
        fee_as_of, fee = fees(codes, today)
    except Exception as e:  # noqa: BLE001
        errors.append("보수 조회 실패: " + _clean_err(e))
        fee_as_of, fee = None, {}
    out = []
    for f in cat:
        item = {k: f.get(k) for k in ("code", "name", "manager", "type", "note")}
        item["bm"] = {k: f["bm"].get(k) for k in ("name", "col", "kind", "lag")}
        item["fees"] = fee.get(f["code"])
        try:
            rows = adjust(history(f["code"], HIST_START, today), sett.get(f["code"], []))
            try:
                bm = bm_series(f["bm"])
            except Exception as e:  # noqa: BLE001 — BM 이 없어도 펀드 지표는 보여 준다
                errors.append(f"{f['code']} BM {f['bm'].get('col')}: " + _clean_err(e))
                bm = None
            item.update(fund_metrics(rows, bm, int(f["bm"].get("lag", 1))))
            item["settlements"] = [{"end": e, "factor": round(b / a, 6)} for e, b, a in sett.get(f["code"], []) if e >= rows[0]["date"]]
            item["ok"] = True
        except Exception as e:  # noqa: BLE001 — 한 펀드 실패가 나머지를 막지 않게
            errors.append(f"{f['code']}: " + _clean_err(e))
            item["ok"] = False
        out.append(item)
    ok = [x for x in out if x["ok"]]
    d = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
         "meta": {"as_of": max((x["nav_date"] for x in ok), default=None), "fee_as_of": fee_as_of,
                  "hist_start": HIST_START.isoformat(), "sources": [SOURCE], "n_ok": len(ok), "n_total": len(out), "errors": errors,
                  "notes": ["예시 공모펀드 — 실제 위탁 계약과 무관.", "BM 은 저장소의 Refinitiv·금투협 이력(가격 지수는 배당 제외).",
                            "보수는 협회 공시 기준일 값(총보수 = 운용+판매+수탁+사무관리, 실부담 = 총보수+기타비용+매매·중개수수료)."]},
         "method": METHOD, "funds": out}
    if save:
        if not ok:
            raise RuntimeError("펀드를 하나도 계산하지 못함 — 기존 funds.json 유지: " + "; ".join(errors))
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
    if not d or not d.get("generated_at"):
        return None
    return (dt.datetime.now() - dt.datetime.fromisoformat(d["generated_at"])).total_seconds() / 3600


def run(force: bool = False) -> str:
    """refresh_all 단계 — 20시간 안에 만든 결과가 있으면 건너뛴다."""
    age = age_hours()
    if not force and age is not None and age < STALE_HOURS:
        return f"건너뜀({age:.0f}시간 전 갱신)"
    d = build()
    return f"펀드 {d['meta']['n_ok']}/{d['meta']['n_total']} · 기준 {d['meta']['as_of']}"


if __name__ == "__main__":
    print(run(force="--force" in sys.argv))
    for x in (load() or {}).get("funds", []):
        if x.get("ok"):
            print(f"{x['code']} {x['nav_date']} 기준가 {x['nav']} 1M {x['returns']['1M']} BM {x['bm_returns']['1M']} "
                  f"초과 {x['excess']['1M']} 순자산 {x['aum_eok']}억 MDD1Y {x['risk']['mdd_1y']}")
    for e in (load() or {}).get("meta", {}).get("errors", []):
        print("오류:", e)
