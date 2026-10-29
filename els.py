# -*- coding: utf-8 -*-
"""ELS 화면 — 공모 ELS 목록(청약중 상품 + 최근 청약종료 상품).

원천: 금융투자협회 전자공시(dis.kofia.or.kr) '파생결합증권 > 청약중인 상품 · 청약종료 상품' XML 서비스(FS-DIS2). POST /proframeWeb/XMLSERVICES/
  청약중   : DISDlsOfferSO ^ selectSubscribing  입력 DISDlsDTO val1 기초자산명 · val2 투자기간 · val3 기초자산수 · val4 기초자산유형 ·
             val5 회사코드(모두 빈칸 = 전체) · val6 탭(0 전체 · 1 ELS · 2 DLS · 3 ELB · 4 DLB) — 날짜 조건 없음, 한 번에 전부(≈ 320건 · 0.4MB).
  청약종료 : DISDlsOfferEndSO ^ getEndList     위와 같은 val1~6 + val7·val8 발행일 시작·끝(YYYYMMDD, 사이트는 3년 이내만). 한 달 ≈ 600건 · 0.7MB.
  (입력 항목은 사이트 화면 /wq/etcann/inc/DISDLSSubscribing.xml · DISDLSSubscribed.xml 의 goSearch() 그대로. val6=1 이 ELS 탭.)
  응답 행 = <DISDlsDTO> 블록(val1~val30 + fileNm · serverPath · originalFileNm). 값은 HTML 이스케이프돼 있고 val8·val18 에 <br/> 가 섞인다.
열 ↔ val 대응(머리글 순서로 짐작하지 않는다 — 머리글에 없는 val26 이 발행일 자리에 끼어 있다):
  출력 메타 DISComOutputMetaSO ^ selectMetaInfo(serviceId SDIS04007001000 청약중 · SDIS04007002000 청약종료, 둘 다 같은 표 DIS0200/DIS0205)의
  DISComOutColHdrDTO(머리글 28개)와 DISComOutDataColDTO.outputColCd(데이터 열 28개)를 순서대로 짝지은 것이 사이트 표의 바인딩이다(2026-10-29 확인).
    val2 발행회사코드 · val3 투자기간(년) · val4 발행회사 · val5 신용등급 · val6 상품명 · val7 기초자산수 · val8 기초자산(<br/> 로 이음) ·
    val9~13 기초자산명1~5 · val26 발행일 · val14 만기일 · val15 조건 충족시 수익률(연, %) · val23 최대손실률(%) · val16 청약시작일 ·
    val17 청약종료일 · val18 상품유형(구조 글) · val20 홈페이지 · val21 비고 · val22 상품코드(ISIN) · originalFileNm · fileNm · serverPath.
  사이트링크 버튼은 pop.js goHomePagePop 이 표 DIS0200·DIS0205 에서 val20 을 연다(http 가 없으면 붙인다).
  간이투자설명서는 pop.js goFileDownloadPop(기본 분기: fileNm · serverPath · originalFileNm) → disCommon.js downloadUrl =
    https://disdown.kofia.or.kr/COMFSFileDownload.jsp?serverPath=…&serverFileNm=<fileNm>&filename=<originalFileNm>. (dis.kofia.or.kr + 경로는 307 로 HTML.)
    disdown 서버는 중간 인증서(GlobalSign RSA OV SSL CA 2018)를 보내지 않아 브라우저(AIA 로 받아 옴)는 열리지만 requests 는 실패한다 —
    여기서는 링크 문자열만 만들고 PDF 를 받지 않는다.
  대조(간이투자설명서 PDF 와): 키움증권 제4247회(KR6KW0006216) 청약 2026-09-17~10-01 · 발행 10-02(val26) · 만기 2029-10-04(val14) ·
    연 10.40%(val15) · KI45 · 최대손실 −100%(val23) / 한국투자증권 트루온 제755회(KR6KS00093P1) 청약 10-19~10-29 · 발행 10-29(val26) ·
    만기 2029-10-29(val14) · 연 12.00% · KI50. 둘 다 일치.
구조 글(val18) 풀기 — 발행사마다 쓰는 법이 달라 깔끔히 읽히는 것만, 못 읽으면 빈칸(None):
  노낙인 = 'noKI · NO KI · NO-KI · 노낙인 · KI 없음' / 낙인 = 'KI45 · KI 35% · KI_35 · 35 KI · knock in 40 · 낙인 35 · /50(종가)(한투) · 45%-(75,…)(삼성)'.
  둘 다 없으면 '확인 불가'(예: 하나증권 'Cpn=13.70%' 만 적힌 행). 조기상환 배리어 = 같은 구분자(- / ,)로 이어진 두 자리 숫자 3개 이상의 첫 묶음
  ('(L75)' 같은 리자드 표시는 뺀다). 조기상환 주기 = 'N개월 · NM · /Nm' 중 12 이하('만기 9개월'·'36개월 만기'처럼 만기에 붙은 숫자, '90(4m)' 같은 괄호 안 관찰 시점은 뺀다).
대상: val6=1(ELS 탭)이 돌려주는 행 그대로 — 이름에 ELS 가 없는 '…파생결합증권'도 이 탭에서 오면 ELS 다. 원금부분지급형(최대손실 −10% 등)도 남긴다.
캐시(data/_raw/kofia/, 저장소 제외): els_end_YYYYMM.json(발행일 기준 달별 청약종료 원본 행). 달이 끝나고 REFETCH_DAYS 가 지난 뒤
  한 번 받은 달은 다시 받지 않는다(늦은 등록·정정 흡수). 이번 달 묶음은 발행일 끝을 오늘 + FUTURE_DAYS 로 늘려 청약은 끝났지만 발행 전인 상품도 담는다.
  청약중은 날마다 새로 받는다. 같은 상품코드가 두 목록에 다 있으면 청약중 쪽만 남긴다.
출력: data/els.json(청약중 · 최근 WINDOW_MONTHS 개월 발행 청약종료). 20시간 안에 만든 결과가 있으면 run() 은 건너뛴다(force=True 면 다시).
사용법: python -X utf8 els.py [--force]
"""
import calendar
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse

import requests

import config

OUT = config.DATA_DIR / "els.json"
CACHE = config.DATA_DIR / "_raw" / "kofia"
URL = "https://dis.kofia.or.kr/proframeWeb/XMLSERVICES/"
HEADERS = {"Content-Type": "application/xml; charset=UTF-8", "User-Agent": "Mozilla/5.0",
           "Referer": "https://dis.kofia.or.kr/websquare/index.jsp"}
DOWNLOAD = "https://disdown.kofia.or.kr/COMFSFileDownload.jsp"
SOURCE = "금융투자협회 전자공시(dis.kofia.or.kr)"
STALE_HOURS = 20
WINDOW_MONTHS = 3                  # 청약종료 상품: 발행일이 최근 이만큼인 것
REFETCH_DAYS = 10                  # 달이 끝나고 이만큼 지나기 전에는 그 달을 다시 받는다
FUTURE_DAYS = 30                   # 이번 달 묶음의 발행일 끝 = 오늘 + 이만큼(청약 끝, 발행 전)
DELAY = 0.5                        # 요청 사이 간격(초) — 협회 서버 예의
TAB_ELS = "1"
METHOD = ("금투협 전자공시 '파생결합증권 청약중인 상품 · 청약종료 상품'의 ELS 탭 목록 그대로(발행사 공시). 수익률 = 조건 충족시 수익률(연, 세전),"
          " 최대손실률 = 공시 값. 구조(조기상환 주기·배리어·낙인)는 상품유형 글에서 깔끔히 읽히는 것만 풀었고, 못 읽으면 빈칸."
          " 청약종료 상품은 발행일이 최근 3개월인 것.")


# ── 금투협 호출·파싱 ──
def _body(svc: str, cond, fn: str, dto: str = "DISDlsDTO") -> bytes:
    c = cond if isinstance(cond, str) else "".join(f"<{k}>{v}</{k}>" for k, v in cond.items())
    return ('<?xml version="1.0" encoding="utf-8"?><message><proframeHeader><pfmAppName>FS-DIS2</pfmAppName>'
            f'<pfmSvcName>{svc}</pfmSvcName><pfmFnName>{fn}</pfmFnName></proframeHeader><systemHeader></systemHeader>'
            f'<{dto}>{c}</{dto}></message>').encode("utf-8")


STATS = {"calls": 0, "bytes": 0}


def _post(svc: str, cond, fn: str, timeout: int = 120) -> str:
    """웹방화벽이 가끔 연결을 끊어 재시도한다."""
    for i in range(5):
        try:
            r = requests.post(URL, data=_body(svc, cond, fn), headers=HEADERS, timeout=(30, timeout))
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


def parse(text: str) -> list[dict]:
    """XML 응답 → DISDlsDTO 행 목록({'val4': ..}). 값의 &amp; 등은 풀어 둔다. 상품명(val6)이 빈 블록(입력 반향)은 뺀다."""
    rows = [{k: html.unescape(v) for k, v in re.findall(r"<(\w+)>([^<]*)</\1>", b)}
            for b in re.findall(r"<DISDlsDTO>(.*?)</DISDlsDTO>", text, re.S)]
    return [r for r in rows if r.get("val6")]


def _cond(**kw) -> dict:
    c = {f"val{i}": "" for i in range(1, 7)}
    c["val6"] = TAB_ELS
    c.update(kw)
    return c


def fetch_subscribing() -> list[dict]:
    return parse(_post("DISDlsOfferSO", _cond(), "selectSubscribing"))


def fetch_ended(start: dt.date, end: dt.date) -> list[dict]:
    return parse(_post("DISDlsOfferEndSO", _cond(val7=_ymd(start), val8=_ymd(end)), "getEndList", timeout=300))


def _ymd(d: dt.date) -> str:
    return f"{d:%Y%m%d}"


def _iso(s) -> str | None:
    s = (s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else None


def _num(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(p, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, p)


def _clean_err(e: Exception) -> str:
    return re.sub(r"[A-Za-z]:\\[^\s'\"]+|/(?:home|Users|mnt|tmp)/[^\s'\"]+", lambda m: re.split(r"[\\/]+", m.group(0))[-1], f"{type(e).__name__}: {e}")


# ── 구조 글 풀기 ──
_NO_KI = re.compile(r"no[\s_-]?ki|non[\s_-]?ki|노\s?낙인|KI\s*없음", re.I)
_KI = [re.compile(p) for p in (r"KI\s*[_:]?\s*(\d{2})(?!\d)", r"(?<!\d)(\d{2})\s*%?\s*KI\b", r"(?i:knock[\s-]?in)\s*(\d{2})(?!\d)",
                                r"낙인\s*(\d{2})(?!\d)", r"/\s*(\d{2})\s*\(종가\)", r"(?<!\d)(\d{2})\s*%\s*-\s*\(")]
_LIZ = re.compile(r"\(\s*L\s*\d{2}\s*\)")
_PERIOD = re.compile(r"(?<!만기)(?<!\()(?<!\d)(\d{1,2})\s*(?:개월|M(?![A-Za-z])|m(?![A-Za-z]))(?!\s*만기)")


def _barriers(t: str) -> list[int] | None:
    t = _LIZ.sub("", t)
    best = None
    for sep in ("-", "/", ","):
        s = re.escape(sep)
        m = re.search(rf"(?<!\d)\d{{2,3}}(?:\s*{s}\s*\d{{2,3}}(?!\d)){{2,}}", t)
        if m and (best is None or m.start() < best.start()):
            best = m
    return [int(x) for x in re.findall(r"\d{2,3}", best.group(0))] if best else None


def parse_structure(text: str) -> dict:
    """상품유형 글 → {ki: 낙인 수준(%) | None, no_ki: 노낙인 여부, barriers: '85-85-80-…' | None, first_barrier, period_m}."""
    t = text or ""
    no_ki = bool(_NO_KI.search(t))
    ki = None
    if not no_ki:
        for p in _KI:
            m = p.search(t)
            if m:
                ki = int(m.group(1))
                break
    bs = _barriers(t)
    tp = re.sub(r"(?<![년yY])만기\s*\d{1,2}\s*개월", "", t)      # '만기 9개월'(만기가 개월인 상품)은 주기가 아니다 — '3년만기 6개월'은 남긴다
    per = next((int(m.group(1)) for m in _PERIOD.finditer(tp) if 1 <= int(m.group(1)) <= 12), None)
    return {"ki": ki, "no_ki": no_ki, "barriers": "-".join(map(str, bs)) if bs else None,
            "first_barrier": bs[0] if bs else None, "period_m": per}


# ── 행 정리 ──
def _br(s) -> str:
    return re.sub(r"\s*<br\s*/?>\s*", " / ", s or "", flags=re.I).strip()


def _url(s) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s if s.lower().startswith("http") else "http://" + s


def doc_url(r: dict) -> str | None:
    """간이투자설명서 내려받기 주소(사이트 downloadUrl 과 같은 형식) — 파일 정보가 하나라도 비면 None."""
    fn, sp, org = ((r.get(k) or "").strip() for k in ("fileNm", "serverPath", "originalFileNm"))
    if not (fn and sp and org):
        return None
    return DOWNLOAD + "?" + urllib.parse.urlencode({"serverPath": sp, "serverFileNm": fn, "filename": org})


def under_name(s: str) -> str:
    return re.sub(r"\s+Index$", "", s.strip())


def row(r: dict) -> dict:
    """원본 행 → 화면 행. 기초자산 이름은 ' Index' 를 떼고(이름이 Index 로 끝나면 지수), 지수가 아닌 것은 stocks 에도 둔다."""
    raw = [r[f"val{i}"].strip() for i in range(9, 14) if (r.get(f"val{i}") or "").strip()]
    if not raw:
        raw = [x for x in (s.strip() for s in re.split(r"<br\s*/?>", r.get("val8") or "", flags=re.I)) if x]
    n_idx = sum(x.endswith("Index") for x in raw)
    kind = None if not raw else "지수" if n_idx == len(raw) else "종목" if n_idx == 0 else "혼합"
    st = _br(r.get("val18"))
    years = _num(r.get("val3"))
    return {"code": (r.get("val22") or "").strip(), "issuer": (r.get("val4") or "").strip(), "issuer_cd": (r.get("val2") or "").strip(),
            "rating": (r.get("val5") or "").strip(), "name": (r.get("val6") or "").strip(),
            "under": [under_name(x) for x in raw], "stocks": [x for x in raw if not x.endswith("Index")], "under_kind": kind, "years": int(years) if years is not None else None,
            "issue": _iso(r.get("val26")), "maturity": _iso(r.get("val14")),
            "sub_start": _iso(r.get("val16")), "sub_end": _iso(r.get("val17")),
            "coupon": _num(r.get("val15")), "max_loss": _num(r.get("val23")),
            "structure": st, "note": _br(r.get("val21")), "url": _url(r.get("val20")),
            "doc": doc_url(r), "doc_name": (r.get("originalFileNm") or "").strip() or None,
            **parse_structure(st)}


# ── 청약종료 달별 캐시 ──
def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def chunks(today: dt.date) -> tuple[dt.date, list[tuple[str, dt.date, dt.date, dt.date]]]:
    """(창 시작, [(YYYYMM, 발행일 시작, 발행일 끝, 달 끝)]) — 창 시작이 든 달부터 이번 달까지 달 단위. 이번 달 끝은 오늘 + FUTURE_DAYS."""
    since = _add_months(today, -WINDOW_MONTHS)
    out, m = [], since.replace(day=1)
    while m <= today:
        last = m.replace(day=calendar.monthrange(m.year, m.month)[1])
        end = max(last, today + dt.timedelta(days=FUTURE_DAYS)) if last >= today else last
        out.append((f"{m:%Y%m}", m, end, last))
        m = last + dt.timedelta(days=1)
    return since, out


def ended_rows(today: dt.date, errors: list[str]) -> tuple[dt.date, list[dict], list[dict]]:
    """창 안 청약종료 원본 행 — 달이 끝나고 REFETCH_DAYS 가 지난 뒤 받은 캐시는 그대로 쓴다. 받기 실패면 캐시(있으면)로."""
    since, cs = chunks(today)
    rows, info = [], []
    for ym, start, end, last in cs:
        p = CACHE / f"els_end_{ym}.json"
        c = _load(p, None)
        settled = last + dt.timedelta(days=REFETCH_DAYS)
        fresh = c is not None and c.get("to", "") >= _ymd(end) and dt.date.fromisoformat(c["fetched_at"][:10]) >= settled
        cached = fresh
        if not fresh:
            try:
                c = {"fetched_at": dt.datetime.now().isoformat(timespec="seconds"), "from": _ymd(start), "to": _ymd(end),
                     "rows": fetch_ended(start, end)}
                _save(p, c)
            except Exception as e:  # noqa: BLE001 — 한 달 실패는 캐시로(없으면 그 달만 빠짐)
                errors.append(f"청약종료 {ym}: " + _clean_err(e))
                cached = c is not None
        if c is None:
            continue
        rows += c["rows"]
        info.append({"month": ym, "n": len(c["rows"]), "cached": cached, "fetched_at": c["fetched_at"]})
    return since, rows, info


def build(save: bool = True, today: dt.date | None = None) -> dict:
    t0 = time.time()
    STATS.update(calls=0, bytes=0)
    today = today or dt.date.today()
    errors: list[str] = []
    ing = [row(r) for r in fetch_subscribing()]      # 실패하면 그대로 올린다 — 기존 els.json 유지
    codes = {x["code"] for x in ing if x["code"]}
    since, raw, info = ended_rows(today, errors)
    ended, seen = [], set()
    for r in raw:
        x = row(r)
        if not x["issue"] or x["issue"] < since.isoformat() or (x["code"] and (x["code"] in codes or x["code"] in seen)):
            continue
        seen.add(x["code"])
        ended.append(x)
    d = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
         "meta": {"as_of": today.isoformat(), "window": {"from": since.isoformat(), "to": today.isoformat()},
                  "window_months": WINDOW_MONTHS, "sources": [SOURCE], "n_subscribing": len(ing), "n_ended": len(ended),
                  "chunks": info, "runtime_sec": round(time.time() - t0, 1), "requests": dict(STATS), "errors": errors,
                  "notes": ["발행사가 금투협에 올린 공시 그대로 — 수익률은 조건 충족시 연 환산(세전), 조건을 못 채우면 최대손실률까지 잃을 수 있다.",
                            "구조(조기상환 주기·배리어·낙인)는 상품유형 글을 코드로 읽은 것 — 빈칸은 글에서 깔끔히 읽히지 않은 것이고, 정확한 조건은 간이투자설명서로 확인.",
                            "낙인 '확인 불가' = 상품유형 글에 낙인·노낙인 표시가 없는 상품.",
                            "청약종료 상품은 발행일 기준 최근 3개월(청약은 끝났고 발행 전인 상품 포함). 같은 상품코드가 청약중에도 있으면 청약중에만 둔다."]},
         "method": METHOD, "subscribing": ing, "ended": ended}
    if save:
        if not ing and not ended:
            raise RuntimeError("ELS 를 하나도 받지 못함 — 기존 els.json 유지: " + "; ".join(errors[:5]))
        _save(OUT, d)
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
    m = d["meta"]
    return f"청약중 {m['n_subscribing']} · 청약종료 {m['n_ended']}(발행 {m['window']['from']}~) · {m['runtime_sec']:.0f}초"


if __name__ == "__main__":
    print(run(force="--force" in sys.argv))
    m = (load() or {}).get("meta", {})
    print("요청:", m.get("requests"), "· 달별:", [(c["month"], c["n"], "캐시" if c["cached"] else "받음") for c in m.get("chunks", [])])
    for e in m.get("errors", [])[:10]:
        print("오류:", e)
