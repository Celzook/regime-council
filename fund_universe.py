# -*- coding: utf-8 -*-
"""공모펀드 스크리닝 유니버스 — 국내주식 액티브(스타일·테마)·국내주식 인덱스·해외주식(지역·테마). ETF·재간접 제외.

원천(금투협 전자공시 dis.kofia.or.kr, 요청은 funds._post — 2026-10-26 사이트 JS 에서 확인):
  공식 분류 : FS-DIS2 ^ DISFundMngResResSO ^ fundROPCmp ('펀드운용실적비교', 월말 기준 펀드별 — 클래스·모펀드·운용 행 포함)
              입력 DISFundMngResResDTO{standardDt=YYYYMM, manageResBrnCd=소유형 코드, fundGb·mngScl·standardCurr 빈 값}
              소유형(DISMngResBrnSrchSO 트리): 111 국내 주식-일반 · 112 성장주 · 113 섹터 · 114 인덱스 · 181 국내 파생형-증권(주식),
              211~214 해외 같은 구분 · 281 해외 파생형-증권(주식). 행: standardCd, fundNm, manageCompCd, koreanShotNm(운용사),
              vInvestRgnGbNm(주요투자지역 — 해외만), netAsstotAmt(순자산 억원, 정수), ropGb1~11(1·3·6·9개월·1·2·3·4·5년·연초·설정 수익률).
  기준가 일괄: FS-DIS2 ^ DISFundStdPriceSO (funds.snapshot) — 날짜 하나, tmpV7=1 공모, tmpV3=02 주식형 / 06 파생상품.
              → V1 운용사, V2 펀드명, V3 대유형, V4 설정일, V5 설정원본(백만원), V6 기준가, V12 표준코드, V13 운용사코드.
분류(universe/fund_classes.json — 사용자가 고친다):
  1차 = 공식 소유형(+ 해외는 주요투자지역 → 지역 묶음). 국내 일반(111)만 펀드명 키워드로 가치·배당·중소형을 나누고 나머지는 '일반'.
  테마(113·213)는 그룹 하나에 두고 펀드명 키워드로 테마 이름(theme)만 붙인다. 181·281 은 이름에 '인덱스'가 있는 것만.
  제외: 펀드명 키워드(ETF '상장지수', 재간접·TDF·EMP·TIF, 사모, 모펀드, 레버리지·인버스) · 대유형 '재간접' · 운용 클래스(아래).
클래스 묶기: 금투협에 클래스 관계 필드가 없다(확인한 서비스 3개 — 기준가 일괄·운용실적·보수 공시). 펀드명에서 클래스 꼬리
  (ClassC-e, 종류A, (C-e), _Ce, C-P2e형, (퇴직연금) …)를 떼어 낸 이름 + 운용사코드가 같으면 같은 펀드. 이름은 표기 차이
  (공백·밑줄·[]와 ()·전각 문자)를 없앤 키로 비교한다(stem_key — 같은 펀드의 운용 계정만 '(주식)', 클래스는 '[주식]'으로 적힌 예가 있다).
  운용 계정(종류형 펀드의 모 클래스 — 판매 클래스들의 합을 담는 계정)은 클래스 합계와 겹치므로 뺀다(operating_accounts):
    이름이 '(운용)'·'…운용'·'(운)'으로 끝나는 행, 클래스가 여럿인 묶음에서 클래스 꼬리가 없거나 꼬리가 '모'인 행,
    그리고 이름으로 운용 계정을 못 찾은 묶음에서만 순자산이 나머지 클래스 합과 SUM_TOL 안으로 같은 행(나머지 2개 이상·합 SUM_MIN_EOK 억 이상).
    (이름으로 이미 찾은 묶음에 합 규칙을 또 쓰면 가장 큰 판매 클래스가 우연히 나머지 합과 비슷해 잘못 빠진다 — 실측 19건.)
    클래스가 하나뿐인 펀드는 그대로 둔다.
    (2026-10-27 검증 레인 지적: 꼬리 없는 운용 행이 합계에 들어가 순자산이 약 2배로 잡혔다 — 에셋플러스글로벌리치투게더 1 운용 10,740.0억 vs
    다른 클래스 합 10,730.7억.)
  대표 클래스 = C-e(설정 1년 이상) → 없으면 설정 1년 이상 판매 클래스 중 순자산 최대(운용 계정은 후보가 아니다). 펀드 설정일 = 클래스 설정일 중 가장 이른 날.
  합산 순자산(억원) = Σ 설정원본(백만원) × 기준가 / 1e5. 거르는 조건: 합산 순자산 ≥ screen.min_aum_eok, 설정 후 ≥ screen.min_age_days.
"""
import datetime as dt
import gzip
import json
import re
import unicodedata
from collections import Counter, defaultdict

import config
import universe

CFG_PATH = universe.DIR / "fund_classes.json"
OUT = config.DATA_DIR / "fund_universe.json"
ROP_SVC = ("DISFundMngResResSO", "fundROPCmp")

TAIL = re.compile(r"\s*\((?:퇴직연금|연금저축|개인연금|연금|퇴직|장마|장기|소득공제)\)\s*$")
ASSET = re.compile(r"[\(\[][^\(\)\[\]]*(?:주식|채권|혼합|파생|재간접|국공채)[^\(\)\[\]]*[\)\]]")
KEEP = re.compile(r"^\s*[\(\[](?:UH|H|USD|KRW|환헤지|환노출)[\)\]]")
UMBRELLA = re.compile(r"(?:[\(\[]\s*운\s*[\)\]]|[\(\[]?\s*운용\s*[\)\]]?)\s*$")
SUM_TOL = 0.005         # 다른 클래스 순자산 합과 이 안으로 같으면 운용 계정으로 본다(실측 운용 계정은 0.1~0.3% 차이)
ACCOUNT_CLS = {"", "모", "운", "운용"}
SUM_MIN_EOK = 5         # … 단 다른 클래스 합이 이보다 작으면(반올림 잡음) 보지 않는다
CLS = re.compile(r"""[\s_]*(?:[\(\[]\s*)?(?:class|종류|클래스|C/)?\s*
   (?P<c>(?!H\b|UH\b|USD\b|KRW\b)[A-Z](?:\s(?=\d\s*[\)\]]?\s*(?:형|클래스)?\s*$))?(?:[A-Za-z0-9]|-(?=[A-Za-z0-9가-힣])|퇴직|개인|연금)*)
   \s*(?:형|클래스)?\s*(?:[\)\]])?\s*$""", re.X | re.I)


def load_cfg(path=None) -> dict:
    return json.loads((path or CFG_PATH).read_text(encoding="utf-8"))


def split_class(name: str) -> tuple[str, str]:
    """펀드명 → (클래스 꼬리를 뗀 이름, 클래스 코드). 클래스가 없으면 코드는 ''.
    자산 표기 괄호('(주식)'·'[주식-파생형]' …)가 있으면 그 뒤가 클래스다(바로 뒤의 '(H)'·'(UH)'·'(USD)' 는 펀드 이름에 남긴다).
    없으면 끝의 클래스 모양(ClassC-e·종류A·(C-e)·_Ce …)을 뗀다."""
    n = TAIL.sub("", name.strip())
    m = None
    for m in ASSET.finditer(n):
        pass
    if m:
        stem, tail = n[:m.end()], n[m.end():]
        h = KEEP.match(tail)
        if h:
            stem, tail = stem + h.group(0).strip(), tail[h.end():]
        code = re.sub(r"class|종류|클래스|C/|형$|[\s_\(\)\[\]]", "", tail, flags=re.I)
        return stem, code
    m = CLS.search(n)
    if not m or m.start() == 0:
        return n, ""
    return n[:m.start()].rstrip(" _"), m.group("c").replace(" ", "")


def stem_key(stem: str) -> str:
    """묶음 키 — 같은 펀드의 표기 차이(공백·밑줄·[]/()·전각 문자)를 없앤다. 예: '…신탁 1[주식]' = '…신탁1(주식)'."""
    s = unicodedata.normalize("NFKC", stem)
    return re.sub(r"[\s_]", "", s).replace("[", "(").replace("]", ")")


def is_umbrella(name: str) -> bool:
    return bool(UMBRELLA.search(name.strip()))


def operating_accounts(members: list[dict], named: bool = False) -> set[str]:
    """한 펀드 묶음({code, cls, aum_eok}) 안의 운용 계정 코드. 클래스가 하나뿐이면 없음.
    named = 이름이 '운용'·'(운)'인 행을 이 묶음에서 이미 뺐음(그러면 합 규칙은 쓰지 않는다)."""
    if len(members) < 2:
        return set()
    acc = {m["code"] for m in members if m["cls"] in ACCOUNT_CLS}
    rest = [m for m in members if m["code"] not in acc]
    if not acc and not named and len(rest) > 2:
        for m in rest:
            others = sum(x["aum_eok"] for x in rest if x["code"] != m["code"])
            if others >= SUM_MIN_EOK and abs(m["aum_eok"] / others - 1) <= SUM_TOL:
                acc.add(m["code"])
    return acc if len(acc) < len(members) else set()


def hedge_of(name: str) -> str | None:
    """펀드명의 환헤지 표기 — 'UH'(환노출), 'H'(환헤지), 없으면 None."""
    for tag in ("UH", "H"):   # '(H)'·'신탁H(주식)'·'UH[주식]' — '차이나H인덱스'(H주) 같은 이름 속 H 는 아니다
        if re.search(rf"(?<![A-Za-z]){tag}(?=[\)\(\[\]]|$)", name):
            return tag
    return None


def _has(name: str, kws) -> str | None:
    up = name.upper()
    return next((k for k in kws if k.upper() in up), None)


def excluded(name: str, big_type: str, cfg: dict) -> str | None:
    """제외 이유(없으면 None). 대유형이 재간접이면 이름에 없어도 재간접."""
    for rule in cfg["exclude"]:
        if _has(name, rule["keywords"]):
            return rule["reason"]
    if big_type.startswith("재간접"):
        return "재간접(TDF·EMP 포함)"
    return None


def region_of(rgn: str, cfg: dict) -> str:
    for bucket, names in cfg["regions"].items():
        if rgn in names:
            return bucket
    return cfg["other_region"]


def classify(sub: str, name: str, rgn: str, cfg: dict) -> dict | None:
    """공식 소유형 + 펀드명 → {family, group, kind, theme?, region?, index}. 소유형이 대상이 아니거나 require 키워드가 없으면 None."""
    st = cfg["sub_types"].get(sub)
    if not st or (st.get("require") and not _has(name, st["require"])):
        return None
    out = {"family": st["family"], "kind": st["kind"], "index": st["kind"] == "index" or bool(st.get("index")),
           "sub_type": sub, "sub_name": st["name"]}
    if st["family"] == "global" and st["kind"] == "region":
        out["region"] = region_of(rgn, cfg)
        out["group"] = out["region"]
    elif st.get("refine"):
        out["group"] = next((s["group"] for s in cfg["kr_styles"] if _has(name, s["keywords"])), cfg["kr_default_style"])
    else:
        out["group"] = st["group"]
    if st["kind"] == "theme":
        out["theme"] = next((t["label"] for t in cfg["themes"] if _has(name, t["keywords"])), cfg["other_theme"])
        if st["family"] == "global":
            out["region"] = region_of(rgn, cfg)
    return out


def group_id(c: dict) -> str:
    return f"{c['family']}:{c['group']}"


def _is_rep(code: str, cfg: dict) -> bool:
    return code.replace(" ", "") in cfg["rep_class"]


def build(rop: dict[str, list[dict]], snap: dict[str, dict], as_of: dt.date, cfg: dict) -> dict:
    """공식 분류 행(소유형 → 행 목록)과 기준가 일괄(표준코드 → 행)로 유니버스를 만든다. 순수 함수.
    snap 행: {code, name, mgr, mgr_cd, type, setup(YYYY-MM-DD), units(백만원), nav}."""
    reasons: Counter = Counter()
    cls_of: dict[str, dict] = {}          # 표준코드 → 분류(공식 행이 있는 클래스)
    rgn_of: dict[str, str] = {}
    for sub, rows in rop.items():
        for r in rows:
            cls_of.setdefault(r["standardCd"], {"sub": sub, "name": r.get("fundNm", "")})
            rgn_of.setdefault(r["standardCd"], r.get("vInvestRgnGbNm", "-"))
    groups: dict[tuple, list[dict]] = defaultdict(list)
    named: set[tuple] = set()   # 이름으로 운용 계정을 뺀 묶음
    for code, s in snap.items():
        if s.get("type") not in cfg["big_types"]:
            reasons["대상 대유형 아님(혼합·채권·특별자산 파생형 등)"] += 1
            continue
        why = excluded(s["name"], s.get("type", ""), cfg)
        if why:
            reasons[why] += 1
            continue
        if is_umbrella(s["name"]):
            reasons["운용 계정(클래스 합과 중복)"] += 1
            named.add((s["mgr_cd"], stem_key(split_class(UMBRELLA.sub("", s["name"]))[0])))
            continue
        stem, cls = split_class(s["name"])
        groups[(s["mgr_cd"], stem_key(stem))].append({**s, "code": code, "cls": cls, "stem": stem})
    funds, unclassified = [], []
    min_age = as_of - dt.timedelta(days=cfg["screen"]["min_age_days"])
    for (mgr_cd, key), members in groups.items():
        stem = Counter(m["stem"] for m in members).most_common(1)[0][0]   # 화면에 보일 이름 — 가장 많이 쓴 표기
        subs = Counter(cls_of[m["code"]]["sub"] for m in members if m["code"] in cls_of)
        if not subs:
            big = sum(m["units"] * m["nav"] / 1e5 for m in members) >= cfg["screen"]["min_aum_eok"]
            unclassified.append((big, stem))
            reasons["금투협 소유형 없음" + (" — 100억 이상" if big else "")] += 1
            continue
        sub = subs.most_common(1)[0][0]
        for m in members:
            m["aum_eok"] = m["units"] * m["nav"] / 1e5
        acc = operating_accounts(members, (mgr_cd, key) in named)
        if acc:
            reasons["운용 계정(클래스 합과 중복)"] += len(acc)
            members = [m for m in members if m["code"] not in acc]
        rgn = next((rgn_of[m["code"]] for m in members if m["code"] in rgn_of and rgn_of[m["code"]] not in ("-", "")), "-")
        c = classify(sub, stem, rgn, cfg)
        if c is None:
            reasons["대상 소유형 아님(파생형 비인덱스 등)"] += 1
            continue
        aum = sum(m["aum_eok"] for m in members)
        setup = min(m["setup"] for m in members)
        if aum < cfg["screen"]["min_aum_eok"]:
            reasons["합산 순자산 기준 미달"] += 1
            continue
        if setup > min_age.isoformat():
            reasons["설정 1년 미만"] += 1
            continue
        old = [m for m in members if m["setup"] <= min_age.isoformat()]
        rep = next((m for m in sorted(old, key=lambda m: -m["aum_eok"]) if _is_rep(m["cls"], cfg)), None) \
            or max(old, key=lambda m: m["aum_eok"])
        funds.append({"id": rep["code"], "name": stem, "manager": rep["mgr"], "manager_cd": mgr_cd,
                      "rep_code": rep["code"], "rep_class": rep["cls"] or "(단일)", "rep_name": rep["name"],
                      "rep_is_ce": _is_rep(rep["cls"], cfg), "setup": setup, "aum_eok": round(aum, 1),
                      "classes": sorted(m["code"] for m in members), "n_classes": len(members),
                      "hedge": hedge_of(stem), "group_id": group_id(c), **c})
    funds.sort(key=lambda f: (f["family"], f["group"], -f["aum_eok"]))
    count = Counter(f["group_id"] for f in funds)
    return {"as_of": as_of.isoformat(), "n": len(funds), "counts": dict(sorted(count.items())),
            "excluded": dict(reasons.most_common()),
            "unclassified_sample": [n for big, n in sorted(unclassified, key=lambda x: (not x[0], x[1]))][:20],
            "funds": funds}


# ── 공식 분류 받기(월 1회 캐시) ──
def parse_rop(text: str) -> list[dict]:
    return [dict(re.findall(r"<([A-Za-z0-9]+)>([^<]*)</\1>", m))
            for m in re.findall(r"<DISFundMngResResDTO>(.*?)</DISFundMngResResDTO>", text, re.S)]


def _rop_text(ym: str, sub: str, cache) -> str:
    import funds
    p = cache / f"rop_{ym}_{sub}.xml.gz"
    if p.exists():
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return f.read()
    body = (f"<standardDt>{ym}</standardDt><manageResBrnCd>{sub}</manageResBrnCd>"
            "<fundGb></fundGb><mngScl></mngScl><standardCurr></standardCurr>")
    text = funds._post(ROP_SVC[0], body, fn=ROP_SVC[1], dto="DISFundMngResResDTO", timeout=300)
    if "<DISFundMngResResDTO>" in text and "<standardCd>" in text:   # 아직 공시 전 달(0건)은 캐시하지 않는다
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write(text)
    return text


def fetch_rop(today: dt.date, cfg: dict, cache) -> tuple[str, dict[str, list[dict]]]:
    """가장 최근 공시 월(전월부터 거꾸로 3개월)의 소유형별 행."""
    d = today.replace(day=1)
    for _ in range(3):
        d = (d - dt.timedelta(days=1)).replace(day=1)
        ym = f"{d:%Y%m}"
        first = parse_rop(_rop_text(ym, next(iter(cfg["sub_types"])), cache))
        if first:
            out = {next(iter(cfg["sub_types"])): first}
            for sub in list(cfg["sub_types"])[1:]:
                out[sub] = parse_rop(_rop_text(ym, sub, cache))
            return ym, out
    raise RuntimeError("금투협 펀드운용실적비교 최근 3개월 0건 — 서비스 변경 또는 일시 차단")


def save(u: dict) -> None:
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT)


def load() -> dict | None:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
