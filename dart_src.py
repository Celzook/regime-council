# -*- coding: utf-8 -*-
"""OpenDART(opendart.fss.or.kr) — 상장사 모·자회사 지분 관계(페어트레이딩 '모자회사' 후보용).

Refinitiv 에 없어서 쓰는 예외 원천이다(2026-10-27 확인 — 이 계정은 TR.InvestorFullName 등 주주 필드가 빈 값).
키: config.api_key("DART_API_KEY")(환경변수 우선, 없으면 data/.api_keys.json). 일 2만 건 한도.
  corpCode.xml(zip)  고유번호 ↔ 종목코드 ↔ 회사명. 상장사만 쓴다(stock_code 가 있는 행).
  otrCprInvstmntSttus 타법인출자 현황(사업보고서 reprt_code 11011) — 출자 대상(inv_prm)이 종목코드 없이 회사명 글자로만 온다.
                     → 상장사 회사명을 정규화해 맞춘다(_norm). 못 맞춘 이름은 universe/pairs_overrides.json 의
                       name_to_code 로 손으로 잇는다(대부분은 비상장 자회사라 못 맞추는 게 정상).
  지분율 = 기말 잔액 지분율(trmend_blce_qota_rt, %), 보유 주식 수 = trmend_blce_qy. 결산일(stlm_dt)을 기준일로 함께 둔다.
  이름만 같은 비상장 자회사(예: DS단석의 '하이브', 현대자동차의 'HMM')가 상장사로 잘못 이어지므로,
  pair_universe.dart_links 가 보유 주식 수 ÷ 상장 자회사 발행주식 수(Refinitiv)로 지분율을 다시 맞춰 본다.
캐시: data/_raw/dart/corp_codes.json(30일), data/_raw/dart/otr_<사업연도>.json({고유번호: 행 목록}) — '자료 없음'은 30일 뒤 다시 본다.
  정정 공시는 다시 받지 않는다(사업연도 캐시를 지우면 새로 받는다).
  사업보고서는 이듬해 3월 말까지 나오므로 4월 1일부터 직전 연도, 그 전엔 전전 연도를 먼저 본다 — 없으면(013) 한 해 더 앞.
사용법: python -X utf8 dart_src.py   # 상장사 전체 출자 현황을 받아 캐시하고 상장 모·자 관계 수를 찍는다
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

import requests

import config

RAW = config.DATA_DIR / "_raw" / "dart"
CORP_CODES = RAW / "corp_codes.json"
OVERRIDES = config.BASE_DIR / "universe" / "pairs_overrides.json"
BASE = "https://opendart.fss.or.kr/api/"
SOURCE = "OpenDART 타법인출자 현황(사업보고서)"
CORP_MAX_AGE_DAYS = 30
DELAY = 0.08            # 호출 사이 쉼(초) — 일 2만 건 한도 안에서 천천히
NO_DATA = "013"         # 조회된 데이터 없음
EMPTY_KEY = "__empty_at__"   # 캐시 안 '자료 없음' 받은 날 {고유번호: YYYY-MM-DD}
EMPTY_RETRY_DAYS = 30


def _scrub(msg, key: str) -> str:
    """오류 문장 속 URL 의 crtfc_key 값을 가린다 — requests 예외는 요청 URL 을 그대로 담는다."""
    return str(msg).replace(key, "***") if key else str(msg)


def _get(path: str, params: dict, timeout: int = 30) -> requests.Response:
    key = config.api_key("DART_API_KEY")
    p = {"crtfc_key": key, **params}
    for i in range(3):
        try:
            r = requests.get(BASE + path, params=p, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == 2:
                raise requests.RequestException(_scrub(e, key)) from None
            time.sleep(2 * (i + 1))


def _save(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def corp_codes(refresh: bool = False) -> dict:
    """{종목코드 6자리: {"corp_code", "name"}} — 상장사만. 캐시가 30일 안이면 그대로."""
    c = _read(CORP_CODES)
    if c and not refresh:
        age = (dt.datetime.now() - dt.datetime.fromisoformat(c["fetched_at"])).days
        if age < CORP_MAX_AGE_DAYS:
            return c["listed"]
    try:
        z = zipfile.ZipFile(io.BytesIO(_get("corpCode.xml", {}, timeout=120).content))
        root = ET.fromstring(z.read(z.namelist()[0]))
    except Exception as e:  # noqa: BLE001 — 받지 못하면 옛 캐시로
        if c:
            print(f"  경고: DART 고유번호 목록 갱신 실패 — 옛 캐시 사용({e})")
            return c["listed"]
        raise
    listed = {}
    for e in root.iter("list"):
        code = (e.findtext("stock_code") or "").strip()
        if code:
            listed[code] = {"corp_code": e.findtext("corp_code"), "name": (e.findtext("corp_name") or "").strip()}
    _save(CORP_CODES, {"fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "listed": listed})
    return listed


def report_year(today: dt.date | None = None) -> int:
    """먼저 볼 사업연도 — 4월 1일부터 직전 연도(3월 말 제출 기한), 그 전엔 전전 연도."""
    today = today or dt.date.today()
    return today.year - 1 if (today.month, today.day) >= (4, 1) else today.year - 2


def _num(s) -> float | None:
    s = str(s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def investments(corp_code: str, year: int, cache: dict | None = None) -> list[dict] | None:
    """한 회사·한 사업연도의 타법인출자 행(원문 필드 일부). 자료 없음 → [] , 호출 실패 → None(캐시에 남기지 않는다)."""
    if cache is not None and corp_code in cache:
        return cache[corp_code]
    try:
        j = _get("otrCprInvstmntSttus.json", {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": "11011"}).json()
    except (requests.RequestException, ValueError) as e:
        print(f"  경고: DART 출자 현황 실패 {corp_code}/{year} — {e}")
        return None
    if j.get("status") == NO_DATA:     # 자료 없음도 캐시하되 언제 봤는지 남긴다 — EMPTY_RETRY_DAYS 뒤 다시(늦은 제출·비12월 결산)
        rows = []
        if cache is not None:
            cache.setdefault(EMPTY_KEY, {})[corp_code] = dt.date.today().isoformat()
    elif j.get("status") != "000":
        print(f"  경고: DART {corp_code}/{year} — {j.get('status')} {j.get('message')}")
        return None
    else:
        rows = [{"inv_prm": x.get("inv_prm", ""), "pct": _num(x.get("trmend_blce_qota_rt")), "qy": _num(x.get("trmend_blce_qy")),
                 "purpose": x.get("invstmnt_purps", ""), "stlm_dt": x.get("stlm_dt", ""), "rcept_no": x.get("rcept_no", "")}
                for x in j.get("list", [])]
    if cache is not None:
        cache[corp_code] = rows
    return rows


_STRIP = re.compile(r"\(주\)|㈜|주식회사|\(株\)|\b(co\.?,?\s*ltd|inc|corp(oration)?)\b\.?|[\s·.,\-()]", re.I)   # 영문 법인 표기는 단어 경계만(Incross ≠ ross)


def _norm(name: str) -> str:
    """회사명 비교용 — 법인 표기·공백·문장부호를 빼고 영문은 소문자. '(주)한화' == '한화' == '㈜ 한화'."""
    return _STRIP.sub("", str(name or "")).lower()


def load_overrides() -> dict:
    o = _read(OVERRIDES) or {}
    return {"name_to_code": o.get("name_to_code", {}), "exclude": o.get("exclude", [])}


def links(codes: set[str] | None = None, min_pct: float = 7.0, today: dt.date | None = None,
          progress: bool = False) -> dict:
    """상장 모회사 → 상장 자회사 보통주 지분 ≥ min_pct 관계.
    codes = 대상 종목코드 집합(모·자 둘 다 이 안에 있어야 함 — 보통주만 넘긴다). None 이면 상장사 전체.
    반환 {"links": [{parent, child, parent_name, child_name, pct, qy(기말 보유 주식 수), as_of, rcept_no, purpose, year}],
          "unmatched": [{parent, inv_prm, pct}] (min_pct 이상인데 상장사로 못 맞춘 이름 — 비상장이 대부분),
          "year", "n_corps", "n_failed", "source"}"""
    listed = corp_codes()
    over = load_overrides()
    by_name = {}
    # DART 목록엔 상장폐지 회사도 종목코드를 단 채 남아 있어 이름이 겹친다(2026-10-28 23개) — codes(현재 상장 대상)에 있는 쪽이 이긴다
    for code, v in sorted(listed.items(), key=lambda kv: codes is not None and kv[0] in codes):
        by_name[_norm(v["name"])] = code
    for nm, code in over["name_to_code"].items():
        by_name[_norm(nm)] = code
    targets = sorted(c for c in listed if codes is None or c in codes)
    y0 = report_year(today)
    caches = {y: (_read(RAW / f"otr_{y}.json") or {}) for y in (y0, y0 - 1)}
    out, unmatched, failed, dirty = [], [], 0, set()
    for i, code in enumerate(targets):
        cc = listed[code]["corp_code"]
        rows, year = None, None
        for y in (y0, y0 - 1):
            if caches[y].get(cc) and "qy" not in caches[y][cc][0]:   # 옛 캐시(보유 주식 수 없음) — 다시 받는다
                del caches[y][cc]
            seen = caches[y].get(EMPTY_KEY, {}).get(cc)
            if caches[y].get(cc) == [] and (seen is None or (dt.date.today() - dt.date.fromisoformat(seen)).days >= EMPTY_RETRY_DAYS):
                del caches[y][cc]                                       # '자료 없음'이 오래됨 — 다시 본다
            had = cc in caches[y]
            r = investments(cc, y, caches[y])
            if r is None:
                break
            if not had:
                dirty.add(y)
                time.sleep(DELAY)
            if r:
                rows, year = r, y
                break
        if rows is None:
            failed += r is None
            continue
        for x in rows:
            if x["pct"] is None or x["pct"] < min_pct:
                continue
            child = by_name.get(_norm(x["inv_prm"]))
            if child is None:
                unmatched.append({"parent": code, "inv_prm": x["inv_prm"], "pct": x["pct"]})
                continue
            if child == code or (codes is not None and child not in codes):
                continue
            out.append({"parent": code, "child": child, "parent_name": listed[code]["name"],
                        "child_name": listed.get(child, {}).get("name", x["inv_prm"]), "pct": x["pct"],
                        "qy": x.get("qy"), "as_of": x["stlm_dt"], "rcept_no": x["rcept_no"], "purpose": x["purpose"], "year": year})
        if progress and (i + 1) % 200 == 0:
            print(f"  DART {i + 1}/{len(targets)}")
            for y in dirty:
                _save(RAW / f"otr_{y}.json", caches[y])
            dirty.clear()
    for y in dirty:
        _save(RAW / f"otr_{y}.json", caches[y])
    # 같은 모·자가 두 줄이면(보통·우선 따로 공시 등) 지분율 큰 쪽 하나
    best = {}
    for l in out:
        k = (l["parent"], l["child"])
        if k not in best or l["pct"] > best[k]["pct"]:
            best[k] = l
    return {"links": sorted(best.values(), key=lambda l: -l["pct"]), "unmatched": unmatched, "year": y0,
            "n_corps": len(targets), "n_failed": failed, "source": SOURCE}


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = links(progress=True)
    print(f"사업연도 {d['year']} 기준 · 상장사 {d['n_corps']}곳 · 실패 {d['n_failed']} · 상장 모·자 {len(d['links'])}건 · 못 맞춘 이름(≥7%) {len(d['unmatched'])}건")
    for l in d["links"][:15]:
        print(f"  {l['parent_name']} → {l['child_name']} {l['pct']}% ({l['as_of']})")
