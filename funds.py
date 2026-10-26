# -*- coding: utf-8 -*-
"""운용사·펀드 화면 — 공모펀드 스크리닝(국내주식 액티브 스타일·테마, 국내주식 인덱스, 해외주식 지역·테마).

유니버스·분류·클래스 묶기는 fund_universe.py(공식 소유형 + universe/fund_classes.json). 여기서는 받아서 성과를 계산한다.
원천: 금융투자협회 전자공시(dis.kofia.or.kr) XML 서비스(FS-DIS2). POST /proframeWeb/XMLSERVICES/
  기준가 일괄 : DISFundStdPriceSO    날짜 하나 → 전 펀드(tmpV7=1 공모, tmpV3=02 주식형 ≈ 12MB · 06 파생상품 ≈ 2MB, 휴일은 0건)
                → V1 운용사, V2 펀드명, V3 대유형, V4 설정일, V5 설정원본(백만원), V6 기준가, V12 표준코드, V13 운용사코드
  기준가 이력 : DISFundStdPrcStutSO  펀드 1개/요청, tmpV30~31 기간, tmpV10=0 일간, tmpV12 표준코드 → V1 기준일, V2 기준가, V5 설정원본
                (대표 클래스 이력을 처음 한 번 채울 때만 — 이후에는 날마다 받는 일괄 스냅샷에서 이어 붙인다)
  결산·분배   : DISFundRdmpSO        기간 필터만 동작(전 펀드가 와서 1년 창 ≈ 48MB) — 분배가 있었던 행(결산전 ≠ 결산후)만 캐시
                → V4 회계기간 종료일, V7 결산전 기준가, V9 결산후 기준가(상환이면 0), V11 코드
  보수        : DISFundFeeCmsSO      tmpV30 기준일 하나 → 전 펀드. 운용보수 V5, 판매 V6, 수탁 V7, 사무관리 V8, 총보수 V9, 기타비용 V11,
                매매·중개수수료 V16 (%/년), 표준코드 V15. 모든 월말이 공시 기준일은 아니라 최근 월말부터 차례로 시도한다.
  (요청 형식·수정기준가 계산은 public_KPI/tools/kofia_tdf/kofia_tdf.py — 인포맥스 수정기준가와 대조 끝난 코드를 옮겼다.)

계산(펀드 = 클래스 묶음, 수익률·위험은 대표 클래스, 순자산·자금은 클래스 합):
  수정기준가 = 기준가 × Π(결산전/결산후) — 그날보다 앞선 결산분만 곱한다. 순자산(억원) = 설정원본(백만원) × 기준가 / 1e5.
  수익률(1W·1M·3M·YTD·1Y·3Y) = 수정기준가 끝 ÷ 기준 − 1. 기준 = 끝 날짜에서 7일·1·3개월·1·3년 뺀 날 이전의 마지막 기준일,
    YTD 는 올해 첫 기준가(기준가 D 는 D-1 장 마감 가격이라 전년 말 마감이 반영된 값). 금투협 '펀드운용실적비교' 공시와 대조
    (2026-10-26, 2026-09-01 기준 대표 클래스 644개): 1Y·3M 차이 중앙값 0.003%p(98.7%가 0.1%p 이내), YTD 같은 기준으로 99.7%.
    벗어나는 것은 달러 표시(USD) 클래스 — 금투협은 원화 환산, 여기는 기준가(달러) 그대로.
  BM(그룹별, fund_classes.json benchmarks)도 같은 두 기준일. BM 값은 lag=n 이면 기준일 D 보다 앞선 n 번째 거래일 값.
    2026-10-26 실측(그룹별 펀드 최대 25개, 최근 1년 일간 로그 변화 상관의 중앙값): 국내 그룹 lag1 0.87~1.00(lag0 ≈ −0.1),
    해외 미국 lag2 0.70 · 유럽 0.94 · 일본 0.81 · 인도 0.63 · 중국 0.50 · 아시아(AAXJ) 0.42 · 신흥국(EEM) 0.32 — 해외는 아시아도 lag2
    (lag1 은 모두 0.21 이하). 환노출(UH) 펀드는 BM × KRW=X(같은 lag). 초과수익 = 펀드 − BM(%p).
  백분위(pct) = 그룹 안 mid-rank 백분위 — (나보다 낮은 펀드 수 + 같은 펀드 수(나 포함) ÷ 2) ÷ 펀드 수 × 100. 높을수록 좋다.
    rank = 그룹 안 순위(1 = 최고). 기본 정렬은 1년 백분위.
  변동성 = 최근 1년 일간 로그수익률 표준편차 × √252. 최대낙폭 = 최근 1년 고점 대비 최저.
  자금: 순자산 증감 = Σ클래스 순자산 끝 − 기준(억원). 순유입(추정) = Σ클래스 (설정원본 끝 − 기준) × 끝 기준가 / 1e5 (억원).
  차트: 공통 주간 격자(금요일, 최근 CHART_YEARS 년, 마지막 점 = 기준일)에 펀드 수정기준가·BM·그룹 중앙값
    (그룹 중앙값 = 주마다 그룹 펀드 주간 수익률의 중앙값을 이어 붙인 지수). /api/funds/<code> 가 펀드 첫 점 = 100 으로 맞춰 준다.
캐시(data/_raw/kofia/, 저장소 제외): snap/YYYYMMDD.json.gz(일괄 스냅샷 요약), nav_<코드>.json, dist.json(분배), fee_<기준일>.xml.gz,
  rop_<YYYYMM>_<소유형>.xml.gz. 출력: data/funds.json(표), data/fund_charts.json(차트), data/fund_universe.json(유니버스).
  20시간 안에 만든 결과가 있으면 run() 은 건너뛴다(force=True 면 다시).
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

OUT = config.DATA_DIR / "funds.json"
CHARTS = config.DATA_DIR / "fund_charts.json"
CACHE = config.DATA_DIR / "_raw" / "kofia"
HIST_DIR = config.DATA_DIR / "history"
URL = "https://dis.kofia.or.kr/proframeWeb/XMLSERVICES/"
HEADERS = {"Content-Type": "application/xml; charset=UTF-8", "User-Agent": "Mozilla/5.0",
           "Referer": "https://dis.kofia.or.kr/websquare/index.jsp"}
SOURCE = "금융투자협회 전자공시(dis.kofia.or.kr)"
STALE_HOURS = 20
HIST_YEARS = 3                     # 이력·차트 길이(3년 수익률까지)
CHART_YEARS = 3
REFETCH_DAYS = 10                  # 개별 이력 증분 때 마지막 날짜보다 이만큼 앞부터 다시 받는다(정정 반영)
DIST_REFETCH_DAYS = 60             # 늦게 올라오는 결산을 흡수
DIST_WINDOW_DAYS = 365
MAX_SNAP_GAP = 15                  # 이어 붙일 영업일이 이보다 많으면 일괄 대신 펀드별 이력으로 채운다
FEE_MAX_AGE_DAYS = 35
DELAY = 0.5                        # 요청 사이 간격(초) — 협회 서버 예의
SNAP_TYPES = ("02", "06")          # 기준가 일괄 대유형 필터: 주식형 · 파생상품
PERIODS = ("1W", "1M", "3M", "YTD", "1Y", "3Y")
METHOD = ("펀드 = 클래스 묶음(이름에서 클래스 꼬리를 뗀 이름 + 운용사). 수익률·위험은 대표 클래스(C-e, 없으면 순자산 최대)의 수정기준가(분배 반영),"
          " 순자산·자금은 클래스 합. 기간은 달력 기준(직전 기준일). BM 은 같은 두 기준일의 값(국내 D-1·해외 D-2 거래일, 환노출은 원화 환산). YTD 기준은 올해 첫 기준가."
          " 백분위 = 같은 그룹 안 mid-rank(높을수록 좋음), 순위 1 = 최고. 변동성 = 1년 일간 로그수익률 표준편차×√252, 최대낙폭 = 1년 고점 대비 최저."
          " 순유입(추정) = 설정원본 증감 × 기준가.")


# ── 금투협 호출·파싱 ──
def _body(svc: str, cond, fn: str = "select", dto: str = "DISCondFuncDTO") -> bytes:
    c = cond if isinstance(cond, str) else "".join(f"<{k}>{v}</{k}>" for k, v in cond.items())
    return ('<?xml version="1.0" encoding="utf-8"?><message><proframeHeader><pfmAppName>FS-DIS2</pfmAppName>'
            f'<pfmSvcName>{svc}</pfmSvcName><pfmFnName>{fn}</pfmFnName></proframeHeader><systemHeader></systemHeader>'
            f'<{dto}>{c}</{dto}></message>').encode("utf-8")


def parse(text: str) -> list[dict]:
    """XML 응답 → selectMeta 행 목록({'tmpV1': ..}). 값의 &amp; 등은 풀어 둔다."""
    return [{k: html.unescape(v) for k, v in re.findall(r"<(tmpV\d+)>([^<]*)</tmpV\d+>", m)}
            for m in re.findall(r"<selectMeta>(.*?)</selectMeta>", text, re.S)]


STATS = {"calls": 0, "bytes": 0}


def _post(svc: str, cond, timeout: int = 300, fn: str = "select", dto: str = "DISCondFuncDTO") -> str:
    """웹방화벽이 가끔 연결을 끊어 재시도한다."""
    for i in range(5):
        try:
            r = requests.post(URL, data=_body(svc, cond, fn, dto), headers=HEADERS, timeout=(30, timeout))
            r.raise_for_status()
            break
        except requests.RequestException:
            if i == 4:
                raise
            time.sleep(3 * (i + 1))
    STATS["calls"] += 1
    STATS["bytes"] += len(r.content)
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


# ── 기준가 일괄 스냅샷 ──
def snap_rows(rows: list[dict]) -> dict[str, dict]:
    """기준가 일괄 행 → {표준코드: {name, mgr, mgr_cd, type, setup, units(백만원), nav}}. 기준가 0·코드 없는 행은 뺀다."""
    out = {}
    for d in rows:
        nav, code = _num(d.get("tmpV6")), d.get("tmpV12")
        if code and nav > 0:
            u = _num(d.get("tmpV5"))
            out[code] = {"name": d.get("tmpV2", ""), "mgr": d.get("tmpV1", ""), "mgr_cd": d.get("tmpV13", ""),
                         "type": d.get("tmpV3", ""), "setup": _iso(d["tmpV4"]) if d.get("tmpV4") else "9999-12-31",
                         "units": 0.0 if math.isnan(u) else u, "nav": nav}
    return out


def snapshot(day: dt.date, today: dt.date | None = None) -> dict[str, dict]:
    """그날 공모 주식형·파생상품 펀드 전부의 기준가·설정원본. 캐시가 있으면 쓰고, 지난 날의 0건(휴일)도 캐시한다."""
    p = CACHE / "snap" / f"{_ymd(day)}.json.gz"
    if p.exists():
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)
    out, answered = {}, True
    for typ in SNAP_TYPES:
        text = _post("DISFundStdPriceSO", dict(tmpV30=_ymd(day), tmpV7="1", tmpV3=typ, tmpV11=""))
        answered &= "<dbio_total_count_>" in text   # 정상 응답(0건 포함) — 차단·오류 응답의 0건은 휴일로 캐시하지 않는다
        out.update(snap_rows(parse(text)))
    if out or (answered and day < (today or dt.date.today())):
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    return out


def snapshot_on_or_before(target: dt.date, today: dt.date, max_back: int = 10) -> tuple[dt.date | None, dict]:
    """목표일 이전(포함)의 마지막 영업일 스냅샷(주말은 묻지 않는다)."""
    d = target
    for _ in range(max_back + 1):
        if d.weekday() < 5:
            s = snapshot(d, today)
            if s:
                return d, s
        d -= dt.timedelta(days=1)
    return None, {}


# ── 대표 클래스 이력 ──
def nav_rows(rows: list[dict]) -> dict[str, dict]:
    """기준가 이력 서비스 행 → {YYYY-MM-DD: {date, nav, units_mn}}. 기준가가 0·빈 값인 행은 뺀다."""
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


def extend_from_snapshots(code: str, snaps: dict[str, dict]) -> list[dict]:
    """캐시된 이력에 스냅샷(날짜 → 일괄 행) 값을 이어 붙여 저장한다. 같은 날짜는 스냅샷 값으로 덮는다."""
    p = CACHE / f"nav_{code}.json"
    st = _load(p, {"since": None, "rows": []})
    old = {r["date"]: r for r in st["rows"]}
    for day, s in snaps.items():
        r = s.get(code)
        if r:
            old[day] = {"date": day, "nav": r["nav"], "units_mn": r["units"]}
    rows = sorted(old.values(), key=lambda r: r["date"])
    _save(p, {"since": st["since"], "rows": rows})
    return rows


def cache_state(code: str) -> tuple[str | None, str | None]:
    st = _load(CACHE / f"nav_{code}.json", {"since": None, "rows": []})
    return st["since"], (st["rows"][-1]["date"] if st["rows"] else None)


# ── 분배 ──
def dist_rows(rows: list[dict]) -> list[dict]:
    """결산 서비스 행 → 분배가 있었던 결산 [{code, end, before, after}](결산전 ≠ 결산후, 결산후 > 0)."""
    out = []
    for d in rows:
        b, a = _num(d.get("tmpV7")), _num(d.get("tmpV9"))
        if d.get("tmpV11") and d.get("tmpV4") and a > 0 and b > 0 and abs(b - a) > 1e-9:
            out.append({"code": d["tmpV11"], "end": _iso(d["tmpV4"]), "before": b, "after": a})
    return out


def distributions(since: dt.date, until: dt.date) -> dict[str, list[tuple[str, float, float]]]:
    """전 펀드 분배 [(회계기간 종료일, 결산전, 결산후)] — 받은 기간은 dist.json 에 남기고 그 뒤만 다시 받는다(대상 펀드가 바뀌어도 다시 받지 않는다)."""
    p = CACHE / "dist.json"
    st = _load(p, {"from": None, "to": None, "rows": []})
    full = st["from"] is None or st["from"] > since.isoformat()
    rows = {} if full else {(r["code"], r["end"]): r for r in st["rows"]}
    s = since if full else max(since, dt.date.fromisoformat(st["to"]) - dt.timedelta(days=DIST_REFETCH_DAYS))
    while s <= until:
        e = min(until, s + dt.timedelta(days=DIST_WINDOW_DAYS - 1))
        got = parse(_post("DISFundRdmpSO", dict(tmpV30=_ymd(s), tmpV31=_ymd(e)), timeout=600))
        if not got and (e - s).days >= 30:   # 한 달 넘는 창에 전 펀드 결산이 0건이면 차단·오류로 본다
            raise RuntimeError(f"결산 {s}~{e} 0건 — 일시 차단 의심, 캐시는 갱신하지 않음")
        rows.update({(r["code"], r["end"]): r for r in dist_rows(got)})
        s = e + dt.timedelta(days=1)
    st = {"from": since.isoformat() if full else st["from"], "to": until.isoformat(),
          "rows": sorted(rows.values(), key=lambda r: (r["code"], r["end"]))}
    _save(p, st)
    out: dict[str, list] = {}
    for r in st["rows"]:
        out.setdefault(r["code"], []).append((r["end"], r["before"], r["after"]))
    return out


def adjust(hist: list[dict], sett: list[tuple[str, float, float]]) -> list[dict]:
    """수정기준가·순자산(억원)을 붙인다. 결산 종료일 다음 날부터 그 결산의 계수(결산전/결산후)를 곱한다."""
    ev = sorted((e, b / a) for e, b, a in sett)
    return [{**r, "adj_nav": r["nav"] * math.prod(x for e, x in ev if e < r["date"]),
             "aum_eok": r["units_mn"] * r["nav"] / 1e5} for r in hist]


# ── 보수 ──
def fee_rows(rows: list[dict], codes: set[str]) -> dict[str, dict]:
    """보수 공시 행 → {표준코드: 보수(%/년)}."""
    out = {}
    for d in rows:
        c = d.get("tmpV15")
        if c in codes:
            f = {k: _num(d.get(v)) for k, v in (("mgmt", "tmpV5"), ("sales", "tmpV6"), ("trustee", "tmpV7"), ("admin", "tmpV8"),
                                                ("total", "tmpV9"), ("other", "tmpV11"), ("trading", "tmpV16"))}
            f["ter"] = f["total"] + np.nan_to_num(f["other"]) + np.nan_to_num(f["trading"])   # 실부담 = 총보수 + 기타비용 + 매매·중개
            out[c] = {k: (None if math.isnan(v) else round(float(v), 4)) for k, v in f.items()}
    return out


def _month_ends(today: dt.date, n: int) -> list[dt.date]:
    out, d = [], today.replace(day=1) - dt.timedelta(days=1)
    for _ in range(n):
        out.append(d)
        d = d.replace(day=1) - dt.timedelta(days=1)
    return out


def _read_gz(p) -> str:
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return f.read()


def fees(codes: set[str], today: dt.date) -> tuple[str | None, dict[str, dict]]:
    """가장 최근 보수 공시 기준일과 대상 코드의 보수. 캐시가 FEE_MAX_AGE_DAYS 안이면 다시 받지 않는다."""
    cached = sorted(CACHE.glob("fee_*.xml.gz"))
    if cached:
        d = cached[-1].name[4:12]
        if (today - dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))).days <= FEE_MAX_AGE_DAYS:
            return _iso(d), fee_rows(parse(_read_gz(cached[-1])), codes)
    for me in _month_ends(today, 4):
        p = CACHE / f"fee_{_ymd(me)}.xml.gz"
        if p.exists():
            text = _read_gz(p)
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
        return _iso(d), fee_rows(parse(_read_gz(cached[-1])), codes)
    return None, {}


# ── BM ──
def bm_series(spec: dict, hist_dir=None, fx: bool = False) -> pd.Series:
    """BM 수준 시계열(날짜 오름차순). fx=True 면 KRW=X(원/달러)를 곱해 원화로 바꾼다(같은 날짜 기준, 직전 값으로 채움)."""
    path = (hist_dir or HIST_DIR) / spec["file"]
    cols = ["date", spec["col"]] + (["KRW=X"] if fx else [])
    df = pd.read_csv(path, index_col=0, parse_dates=True, usecols=cols).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    s = pd.to_numeric(df[spec["col"]], errors="coerce")
    if fx:
        s = s * pd.to_numeric(df["KRW=X"], errors="coerce").ffill()
    s = s.dropna()
    return s[s > 0]


def align_bm(dates: pd.DatetimeIndex, bm: pd.Series, lag: int) -> pd.Series:
    """펀드 기준일마다 BM 값 — lag=0 이면 그날까지의 마지막 거래일, lag=n(≥1) 이면 그날보다 앞선 n 번째 거래일."""
    pos = bm.index.searchsorted(dates, side="left" if lag else "right") - max(lag, 1)
    vals = np.where(pos >= 0, bm.to_numpy()[np.clip(pos, 0, None)], np.nan)
    return pd.Series(vals, index=dates)


# ── 지표(순수 함수) ──
def base_date(idx: pd.DatetimeIndex, end: pd.Timestamp, period: str) -> pd.Timestamp | None:
    """기간 시작 기준일 = 목표일 이전(포함)의 마지막 기준일. 이력이 목표일보다 늦게 시작하면 None."""
    if period == "YTD":   # 올해 첫 기준가(= 전년 말 장 마감 반영) — 금투협 '연초 이후'와 같은 기준(2026-10-26 대조)
        first = idx[idx >= pd.Timestamp(end.year, 1, 1)]
        return first[0] if len(first) and len(idx[idx < first[0]]) else None
    target = {"1W": end - pd.Timedelta(days=7), "1M": end - pd.DateOffset(months=1), "3M": end - pd.DateOffset(months=3),
              "1Y": end - pd.DateOffset(years=1), "3Y": end - pd.DateOffset(years=3)}[period]
    if len(idx) == 0 or idx[0] > target:
        return None
    return idx[idx.searchsorted(target, side="right") - 1]


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


def period_returns(nav: pd.Series, b: pd.Series | None) -> tuple[dict, dict, dict]:
    """수정기준가·(펀드 날짜에 맞춘) BM → 기간별 (펀드, BM, 초과) 수익률(%)."""
    end = nav.index[-1]
    ret, bret, exc = {}, {}, {}
    for p in PERIODS:
        d0 = base_date(nav.index, end, p)
        ret[p] = _pct(nav[end], nav[d0]) if d0 is not None else None
        bret[p] = _pct(b[end], b[d0]) if d0 is not None and b is not None else None
        exc[p] = round(ret[p] - bret[p], 2) if ret[p] is not None and bret[p] is not None else None
    return ret, bret, exc


def fund_metrics(rows: list[dict], bm: pd.Series | None, lag: int = 1) -> dict:
    """adjust() 결과 행(오름차순)과 BM 수준 시계열 → 수익률·위험 지표. 네트워크·파일 없음."""
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["date"])
    nav = df["adj_nav"].astype(float)
    end = nav.index[-1]
    b = align_bm(nav.index, bm, lag) if bm is not None and len(bm) else None
    ret, bret, exc = period_returns(nav, b)
    y1 = nav[nav.index > end - pd.DateOffset(years=1)]
    last = df.iloc[-1]
    return {"nav_date": end.strftime("%Y-%m-%d"), "nav": round(float(last["nav"]), 2), "adj_nav": round(float(last["adj_nav"]), 4),
            "returns": ret, "bm_returns": bret, "excess": exc,
            "risk": {"vol_1y": vol(y1), "mdd_1y": max_drawdown(y1)}, "history_from": nav.index[0].strftime("%Y-%m-%d")}


def mid_pct(values: dict[str, float | None]) -> tuple[dict[str, float], dict[str, int]]:
    """{id: 값} → (mid-rank 백분위(높을수록 좋음), 순위(1 = 최고)). None 은 빠진다."""
    v = {k: x for k, x in values.items() if x is not None}
    n = len(v)
    xs = sorted(v.values())
    pct, rank = {}, {}
    for k, x in v.items():
        lo = sum(1 for y in xs if y < x)
        eq = sum(1 for y in xs if y == x)
        pct[k] = round((lo + eq / 2) / n * 100, 1)
        rank[k] = sum(1 for y in xs if y > x) + 1
    return pct, rank


def _q(xs: list[float], q: float) -> float | None:
    return round(float(np.percentile(xs, q)), 2) if xs else None


def class_flows(classes: list[str], end: dict, base: dict) -> dict:
    """클래스 합 순자산·설정원본 끝/기준 → {aum_chg_eok, aum_chg_pct, units_chg_pct, flow_eok(추정 순유입)}."""
    aum_e = sum(end[c]["units"] * end[c]["nav"] / 1e5 for c in classes if c in end)
    aum_b = sum(base[c]["units"] * base[c]["nav"] / 1e5 for c in classes if c in base)
    u_e = sum(end[c]["units"] for c in classes if c in end)
    u_b = sum(base[c]["units"] for c in classes if c in base)
    flow = sum((end[c]["units"] - base.get(c, {}).get("units", 0.0)) * end[c]["nav"] / 1e5 for c in classes if c in end)
    return {"aum_chg_eok": round(aum_e - aum_b, 1) if base else None, "aum_chg_pct": _pct(aum_e, aum_b) if base else None,
            "units_chg_pct": _pct(u_e, u_b) if base else None, "flow_eok": round(flow, 1) if base else None}


# ── 차트(주간 격자) ──
def week_grid(end: pd.Timestamp, years: int = CHART_YEARS) -> pd.DatetimeIndex:
    """금요일 주간 격자(마지막 점 = 기준일)."""
    g = pd.date_range(end - pd.DateOffset(years=years), end, freq="W-FRI")
    return g if len(g) and g[-1] == end else g.append(pd.DatetimeIndex([end]))


def on_grid(s: pd.Series, grid: pd.DatetimeIndex) -> pd.Series:
    """격자 날짜마다 그 날 이전(포함)의 마지막 값. 이력 시작 전은 NaN."""
    s = s.dropna().sort_index()
    pos = s.index.searchsorted(grid, side="right") - 1
    return pd.Series(np.where(pos >= 0, s.to_numpy()[np.clip(pos, 0, None)], np.nan), index=grid)


def median_index(levels: list[pd.Series]) -> pd.Series:
    """같은 격자 위 펀드 수준들 → 주마다 주간 수익률 중앙값을 이어 붙인 지수(첫 점 100)."""
    df = pd.concat(levels, axis=1)
    med = df.pct_change(fill_method=None).median(axis=1, skipna=True).fillna(0)
    return 100 * (1 + med).cumprod()


def _arr(s: pd.Series, nd: int = 2) -> list:
    return [None if not np.isfinite(x) else round(float(x), nd) for x in s.to_numpy(dtype=float)]


def detail(charts: dict, code: str, item: dict | None = None) -> dict | None:
    """차트 문서에서 펀드 하나의 상세 시계열 — 펀드 첫 점 = 100, BM·그룹 중앙값도 그 점 = 100."""
    f = (charts or {}).get("funds", {}).get(code)
    if not f:
        return None
    vals = f["v"]
    i0 = next((i for i, x in enumerate(vals) if x is not None), None)
    if i0 is None:
        return None

    def rebase(xs):
        if not xs or xs[i0] in (None, 0):
            return None
        return [None if (j < i0 or x is None) else round(x / xs[i0] * 100, 2) for j, x in enumerate(xs)]
    return {"code": code, "name": (item or {}).get("name"), "group_id": f["g"], "dates": charts["dates"],
            "fund": rebase(vals), "bm": rebase(charts.get("bm", {}).get(f.get("b") or "")),
            "bm_name": f.get("bn"), "group_median": rebase(charts.get("median", {}).get(f["g"]))}


# ── 조립 ──
def _clean_err(e: Exception) -> str:
    return re.sub(r"[A-Za-z]:\\[^\s'\"]+|/(?:home|Users|mnt|tmp)/[^\s'\"]+", lambda m: re.split(r"[\\/]+", m.group(0))[-1], f"{type(e).__name__}: {e}")


def _bm_key(f: dict, cfg: dict) -> tuple[str | None, dict | None, bool]:
    gid = f["group_id"]
    spec = cfg["benchmarks"].get(gid, cfg["benchmarks"].get(f["family"]))
    if not isinstance(spec, dict):
        return None, None, False
    fx = bool(spec.get("fx_unhedged")) and f.get("hedge") == "UH"
    return f"{spec['col']}{'*KRW' if fx else ''}", spec, fx


def latest_day(today: dt.date) -> tuple[dt.date, dict]:
    d, s = snapshot_on_or_before(today, today)
    if d is None:
        raise RuntimeError("최근 10일 기준가 일괄 0건 — 일시 차단 또는 서비스 변경")
    return d, s


def _rep_histories(reps: list[str], since: dt.date, as_of: dt.date, latest: dict, today: dt.date, errors: list) -> dict[str, list[dict]]:
    """대표 클래스 이력 — 처음이면 펀드별 이력, 이후에는 일괄 스냅샷(빠진 영업일만 받는다)으로 이어 붙인다."""
    states = {c: cache_state(c) for c in reps}
    fresh = [c for c, (s, last) in states.items() if not s or s > since.isoformat() or not last]
    lasts = [dt.date.fromisoformat(last) for c, (s, last) in states.items() if c not in fresh]
    snaps = {}
    if lasts:
        days = [d.date() for d in pd.bdate_range(min(lasts) + dt.timedelta(days=1), as_of)]
        if len(days) > MAX_SNAP_GAP:
            fresh = reps   # 오래 비었으면 펀드별 증분으로
        else:
            for d in days:
                s = latest if d == as_of else snapshot(d, today)
                if s:
                    snaps[d.isoformat()] = s
    out = {}
    for c in reps:
        try:
            out[c] = history(c, since, as_of) if c in fresh else extend_from_snapshots(c, snaps)
        except Exception as e:  # noqa: BLE001 — 한 펀드 실패가 나머지를 막지 않게
            errors.append(f"{c} 이력: " + _clean_err(e))
    return out


def build(save: bool = True, today: dt.date | None = None) -> dict:
    import fund_universe as FU
    today = today or dt.date.today()
    t0 = time.time()
    STATS.update(calls=0, bytes=0)
    cfg = FU.load_cfg()
    errors: list[str] = []
    as_of, latest = latest_day(today)
    rop_month, rop = FU.fetch_rop(today, cfg, CACHE)
    u = FU.build(rop, latest, as_of, cfg)
    u["rop_month"] = rop_month
    funds_u = u["funds"]
    since = as_of - dt.timedelta(days=365 * HIST_YEARS + 21)
    end_ts = pd.Timestamp(as_of)
    bases = {}
    for p, off in (("1m", pd.DateOffset(months=1)), ("3m", pd.DateOffset(months=3))):
        d, s = snapshot_on_or_before((end_ts - off).date(), today)
        bases[p] = (d.isoformat() if d else None, s)
    try:
        dist = distributions(since, as_of)
    except Exception as e:  # noqa: BLE001 — 결산을 못 받으면 분배 미반영으로라도 보여 준다
        errors.append("결산(분배) 조회 실패 — 수정기준가에 분배 미반영: " + _clean_err(e))
        dist = {}
    try:
        fee_as_of, fee = fees({f["rep_code"] for f in funds_u}, today)
    except Exception as e:  # noqa: BLE001
        errors.append("보수 조회 실패: " + _clean_err(e))
        fee_as_of, fee = None, {}
    hists = _rep_histories([f["rep_code"] for f in funds_u], since, as_of, latest, today, errors)
    bm_cache: dict[str, pd.Series | None] = {}
    bm_lag: dict[str, int] = {}
    grid = week_grid(end_ts)
    levels, out = {}, []
    for f in funds_u:
        item = {k: f[k] for k in ("id", "name", "manager", "manager_cd", "family", "group", "group_id", "kind", "index",
                                  "sub_type", "sub_name", "rep_code", "rep_class", "rep_name", "rep_is_ce", "n_classes",
                                  "setup", "hedge")}
        item["theme"], item["region"] = f.get("theme"), f.get("region")
        item["currency"] = "USD" if re.search(r"[\(\[]USD[\)\]]|달러", f["rep_name"]) else "KRW"   # 달러 표시 클래스는 수익률도 달러 기준
        item["aum_eok"] = f["aum_eok"]
        item["units_eok"] = round(sum(latest[c]["units"] for c in f["classes"] if c in latest) / 100, 1)
        item["fees"] = fee.get(f["rep_code"])
        item["flows"] = {f"{k}_{p}": v for p, (d, s) in bases.items() for k, v in class_flows(f["classes"], latest, s).items()}
        key, spec, fx = _bm_key(f, cfg)
        item["bm"] = {"name": spec["name"] + (" (원화 환산)" if fx else ""), "key": key} if spec else None
        rows = hists.get(f["rep_code"])
        try:
            if not rows:
                raise RuntimeError("이력 없음")
            rows = [r for r in rows if since.isoformat() <= r["date"] <= as_of.isoformat()]
            adj = adjust(rows, dist.get(f["rep_code"], []))
            if key and key not in bm_cache:
                bm_lag[key] = int(spec.get("lag", 1))
                try:
                    bm_cache[key] = bm_series(spec, fx=fx)
                except Exception as e:  # noqa: BLE001 — BM 이 없어도 펀드 지표는 보여 준다
                    errors.append(f"BM {key}: " + _clean_err(e))
                    bm_cache[key] = None
            bm = bm_cache.get(key) if key else None
            item.update(fund_metrics(adj, bm, int(spec.get("lag", 1)) if spec else 1))
            nav = pd.Series([r["adj_nav"] for r in adj], index=pd.to_datetime([r["date"] for r in adj]))
            levels[f["id"]] = (on_grid(nav, grid), f["group_id"], key if bm is not None else None, item["bm"]["name"] if item["bm"] else None)
            item["ok"] = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"{f['rep_code']}: " + _clean_err(e))
            item["ok"] = False
        out.append(item)
    ok = [x for x in out if x["ok"]]
    # 그룹 안 백분위·순위
    by_group: dict[str, list[dict]] = {}
    for x in ok:
        by_group.setdefault(x["group_id"], []).append(x)
    for gid, xs in by_group.items():
        for p in PERIODS:
            pct, rank = mid_pct({x["id"]: x["returns"][p] for x in xs})
            for x in xs:
                x.setdefault("pct", {})[p] = pct.get(x["id"])
                x.setdefault("rank", {})[p] = rank.get(x["id"])
        for x in xs:
            x["n_group"] = len(xs)
    groups = _groups(by_group, cfg)
    managers = _managers(ok)
    charts = {"generated_at": None, "dates": [d.strftime("%Y-%m-%d") for d in grid],
              "funds": {k: {"v": _arr(v[0]), "g": v[1], "b": v[2], "bn": v[3]} for k, v in levels.items()},
              "bm": {}, "median": {}}
    for key, s in bm_cache.items():
        if s is not None:
            charts["bm"][key] = _arr(align_bm(grid, s, bm_lag[key]), 4)
    for gid in by_group:
        lv = [v[0] for k, v in levels.items() if v[1] == gid]
        if lv:
            charts["median"][gid] = _arr(median_index(lv), 4)
    flows = sorted((x for x in ok if x["flows"].get("flow_eok_1m") is not None), key=lambda x: x["flows"]["flow_eok_1m"])
    d = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
         "meta": {"as_of": as_of.isoformat(), "fee_as_of": fee_as_of, "rop_month": rop_month,
                  "flow_base": {p: v[0] for p, v in bases.items()}, "hist_from": since.isoformat(),
                  "sources": [SOURCE], "n_ok": len(ok), "n_total": len(out), "errors": errors,
                  "universe": {k: u[k] for k in ("n", "counts", "excluded", "unclassified_sample")},
                  "families": cfg["families"], "periods": list(PERIODS),
                  "runtime_sec": round(time.time() - t0, 1), "requests": dict(STATS),
                  "notes": ["금투협 공시 기준가·설정원본으로 계산(수익률은 대표 클래스, 보수 차감 후).",
                            "BM 은 저장소의 Refinitiv 이력(가격 지수, 배당 제외). 해외 글로벌·베트남·테마는 맞는 지수가 없어 BM 없음.",
                            "보수는 협회 공시 기준일 값(총보수 = 운용+판매+수탁+사무관리, 실부담 = 총보수+기타비용+매매·중개수수료).",
                            "순유입(추정) = 클래스별 설정원본 증감 × 현재 기준가 — 분배 재투자도 유입으로 잡힌다.",
                            "순자산 = 기준가 일괄 공시에 나온 판매 클래스의 합(운용 계정 제외) — 일괄 공시에 빠진 클래스가 있으면 조금 작게 잡힌다."]},
         "method": METHOD, "groups": groups, "managers": managers,
         "flow_leaders": {"in_1m": [x["id"] for x in flows[::-1][:10]], "out_1m": [x["id"] for x in flows[:10]]},
         "funds": out}
    if save:
        if not ok:
            raise RuntimeError("펀드를 하나도 계산하지 못함 — 기존 funds.json 유지: " + "; ".join(errors[:5]))
        charts["generated_at"] = d["generated_at"]
        FU.save(u)
        for path, doc in ((CHARTS, charts), (OUT, d)):
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
            os.replace(tmp, path)
    return d


def _groups(by_group: dict[str, list[dict]], cfg: dict) -> list[dict]:
    """그룹 요약. BM 수익률은 그룹에서 환산 없는 BM 을 쓰는 첫 펀드의 값(같은 달력·같은 두 기준일이라 펀드마다 같다)."""
    out = []
    for gid, xs in sorted(by_group.items()):
        fam, grp = gid.split(":", 1)
        spec = cfg["benchmarks"].get(gid, cfg["benchmarks"].get(fam))
        bret = next((x["bm_returns"] for x in xs if isinstance(spec, dict) and (x.get("bm") or {}).get("key") == spec["col"]
                     and x["nav_date"] == max(y["nav_date"] for y in xs)), None)
        stats = {}
        for p in PERIODS:
            v = [x["returns"][p] for x in xs if x["returns"][p] is not None]
            stats[p] = {"n": len(v), "median": _q(v, 50), "p25": _q(v, 25), "p75": _q(v, 75)}
        out.append({"id": gid, "family": fam, "family_label": cfg["families"].get(fam, fam), "group": grp, "kind": xs[0]["kind"],
                    "n": len(xs), "aum_eok": round(sum(x["aum_eok"] for x in xs), 1),
                    "bm": {"name": spec["name"], "key": spec["col"]} if isinstance(spec, dict) else None, "bm_returns": bret,
                    "stats": stats, "flow_eok_1m": round(sum(x["flows"].get("flow_eok_1m") or 0 for x in xs), 1),
                    "flow_eok_3m": round(sum(x["flows"].get("flow_eok_3m") or 0 for x in xs), 1)})
    return out


def _managers(ok: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for x in ok:
        by.setdefault(x["manager_cd"], []).append(x)
    out = []
    for cd, xs in by.items():
        fams = {}
        for fam in sorted({x["family"] for x in xs}):
            ps = [x["pct"]["1Y"] for x in xs if x["family"] == fam and x.get("pct", {}).get("1Y") is not None]
            fams[fam] = {"n": sum(1 for x in xs if x["family"] == fam), "avg_pct_1y": round(sum(ps) / len(ps), 1) if ps else None}
        ps = [x["pct"]["1Y"] for x in xs if x.get("pct", {}).get("1Y") is not None]
        out.append({"manager_cd": cd, "manager": xs[0]["manager"], "n": len(xs), "aum_eok": round(sum(x["aum_eok"] for x in xs), 1),
                    "avg_pct_1y": round(sum(ps) / len(ps), 1) if ps else None, "families": fams,
                    "top_quartile_1y": sum(1 for p in ps if p >= 75),
                    "flow_eok_1m": round(sum(x["flows"].get("flow_eok_1m") or 0 for x in xs), 1),
                    "flow_eok_3m": round(sum(x["flows"].get("flow_eok_3m") or 0 for x in xs), 1),
                    "aum_chg_eok_1m": round(sum(x["flows"].get("aum_chg_eok_1m") or 0 for x in xs), 1)})
    return sorted(out, key=lambda m: -m["aum_eok"])


def load() -> dict | None:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_charts() -> dict | None:
    try:
        return json.loads(CHARTS.read_text(encoding="utf-8"))
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
    m = d["meta"]
    return f"펀드 {m['n_ok']}/{m['n_total']} · 기준 {m['as_of']} · {m['runtime_sec']:.0f}초"


if __name__ == "__main__":
    print(run(force="--force" in sys.argv))
    d = load() or {}
    m = d.get("meta", {})
    print("유니버스:", json.dumps(m.get("universe", {}).get("counts"), ensure_ascii=False))
    print("제외:", json.dumps(m.get("universe", {}).get("excluded"), ensure_ascii=False))
    print("요청:", m.get("requests"), "· 오류", len(m.get("errors", [])))
    for e in m.get("errors", [])[:10]:
        print("오류:", e)
