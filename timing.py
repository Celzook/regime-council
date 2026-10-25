# -*- coding: utf-8 -*-
"""마켓타이밍 시그널 표(1차) — 저장소에 이미 있는 데이터로 '지금 위험선호인가 위험회피인가'를 신호별로 판정한다.

신호 정의·방향·임계값·근거 등급·출처는 universe/timing.json(카탈로그)에 있다 — 숫자를 바꿀 때는 그 파일만 고친다.
입력(새 원천 없음, 받지 않고 읽기만 — 갱신은 refresh_all 의 앞 단계가 한다):
  analytics.load_panel()  : data/history/{kofia,yf,fred}_daily.csv + market.json 최신값.
                            spread_aa·spread_bbb(회사채 AA−·BBB− 3년 − 국고 3년), usdkrw(KRW=KFTC), kospi200(.KS200)·kospi,
                            ks200_basis(KSc1 ÷ .KS200 − 1, %), vix(yfinance ^VIX — Refinitiv 권한 없음).
  data/history/equity_daily.csv(equity.py)·timing_daily.csv(spikes.py) : .KSVKOSPI(VKOSPI)·^VIX·^VIX3M — 두 파일을 날짜별로 합친다
                            (^VIX3M 은 refinitiv_src.YF_ONLY 로 timing_daily.csv 에만 쌓인다). market.json equity.vkospi 가 더 최신 날짜면 끝에 잇는다.
  data/equity_valhist.json(valhist.py)      : KOSPI200 월말 PER·PBR(자기 과거 범위).
계산(CODE, 네트워크 없음 — compute() 는 순수 함수):
  변환: level(×scale) · ratio(a ÷ b) · ma_gap(종가 ÷ N일 이동평균 − 1, %) · ret(lag 거래일 전 → skip 거래일 전 수익률, %) · chg_pct(lag 거래일 변화율, %).
  입력 후보가 여럿이면(KOSPI200 → KOSPI) 규칙이 쓰는 지표를 모두 계산할 만큼 이력이 있는 첫 후보를 쓴다.
  백분위 = mid-rank(창 안에서 지금보다 낮은 날 + 같은 날 ½) ÷ 창 길이 — valhist.position·spikes.pct_1y·equity.level_stats 와 같은 정의.
           창은 모든 신호에서 관측치 개수: 1년 = 최근 252개(252개가 차야 — spikes.pct_1y 와 같음), 5년 = 최근 1260개(60개 초과면 계산,
           1260개가 안 되면 pct_5y_from 에 시작일).
  z_1y = (지금 − 최근 252개 평균) ÷ 표준편차(20개 초과·표준편차 > 0). chg_20d = 지금 − 20거래일 전. history_from = 변환 전 원 입력의 시작일.
  밸류에이션 행은 valhist.position(최근 12·60개월)을 그대로 쓴다. 최근 60개월 중 유효한 달이 60개 미만이면 pct_5y_from = 그 창의 첫 유효 달.
  상태: 카탈로그 rule — 위험회피 조건(모두)을 먼저, 아니면 위험선호 조건(모두), 둘 다 아니면 중립. 필요한 지표가 없으면 상태 없음(판정 보류).
        판정은 화면에 보이는 반올림 값으로 한다(VIX 29.996 → 30.0 으로 보이고 30 이상으로 판정) — 보이는 숫자와 상태가 어긋나지 않게.
  신선도: 신호 기준일이 신호들 중 가장 최근 기준일보다 composite.stale_bdays(기본 5) 영업일 넘게 늦으면 stale=true
          (월말 밸류에이션 행은 composite.stale_days_monthly(기본 45) 달력일). 값·상태는 보여 주되 합성에서 뺀다.
  합성 점수: 위험선호 +1·중립 0·위험회피 −1 을 group 안에서 평균 → group 들을 동일가중 평균(최적화 없음). in_composite=false(배경·참고)는 뺀다.
            '구현·데이터 대기' 신호는 분모에서 빼고 waiting 으로만 나열. 경계는 표시값(소수 둘째 자리)에 엄격 부등호(> risk_on, < risk_off).
출력: data/timing.json — 마켓타이밍 탭 '시그널'이 쓴다(W4 연결). 행 계약:
      ok     = 값을 계산했다(value·as_of 있음). false 면 reason(입력 없음·이력 짧음·계산 오류 …).
      judged = 상태(state)를 정했다. ok 이어도 판정 지표가 모자라면 false — 화면은 '보류'로 보여 준다.
      stale  = 기준일이 늦어 합성에서 뺐다(값·상태는 그대로 표시).
      2차 신호(외국인 선물·신용잔고·예탁금·풋콜·차익잔고)는 원천 미확정 — 카탈로그 목록만 phase2 로 옮긴다(코드 없음).

사용법: python timing.py
"""
import datetime
import json
import math
import operator
import os
import sys

import pandas as pd

import config
import universe

OUT = config.DATA_DIR / "timing.json"
CATALOG = universe.DIR / "timing.json"
EQ_HIST = config.DATA_DIR / "history" / "equity_daily.csv"
TIMING_HIST = config.DATA_DIR / "history" / "timing_daily.csv"   # spikes.HIST — spikes 는 refinitiv_src 를 끌어오므로 import 하지 않는다
WAITING = "구현·데이터 대기"
IMPLEMENTED = ("구현", WAITING)
PHASE2 = "원천 미확정"
STATES = ("위험선호", "중립", "위험회피")
DIRECTIONS = ("high_risk_off", "high_risk_on")
METRICS = ("value", "pct_1y", "pct_5y", "z_1y", "chg_20d")
OPS = {"gte": operator.ge, "gt": operator.gt, "lte": operator.le, "lt": operator.lt}
KINDS = ("level", "ratio", "ma_gap", "ret", "chg_pct", "valhist")
PANEL_COLS = ("spread_aa", "spread_bbb", "usdkrw", "kospi200", "kospi", "ks200_basis", "vix")
EQ_COLS = {".KSVKOSPI": "vkospi", "^VIX3M": "vix3m", "^VIX": "vix"}   # equity_daily.csv 열 → 입력 이름(vix 는 패널에 없을 때만)
MIN_PCT, MIN_Z = 60, 20     # analytics.stats 와 같은 최소 개수(초과)
WIN_1Y, WIN_5Y = 252, 1260  # 백분위·z 창 = 관측치 개수(spikes.PCT_WIN·equity.level_stats 와 같음)
STALE_BDAYS, STALE_DAYS_MONTHLY = 5, 45   # 카탈로그 composite 에 없을 때 기본값
METHOD = ("신호별 현재값과 자기 과거 1년(최근 252개)·5년(최근 1260개) 백분위(mid-rank), 1년 z-점수로 위험선호·중립·위험회피를 판정"
          "(임계값은 universe/timing.json, 판정은 화면에 보이는 반올림 값으로). "
          "합성 점수 = 상태 점수(+1·0·−1)를 같은 묶음끼리 평균한 뒤 묶음 동일가중 평균 — 가중치 최적화 없음, 배경·참고·데이터 대기·지연 신호 제외. "
          "백분위·z 는 과거 분포 안 위치일 뿐 수익 예측이 아니며, 근거 등급(A 학술 다수·B 학술 일부·C 실무 관행)을 함께 봅니다.")


# ── 카탈로그 ──
def _conds_ok(conds, where: str) -> list[str]:
    errs = []
    if not isinstance(conds, list) or not conds:
        return [f"{where}: 조건 목록이 비었음"]
    for c in conds:
        ops = [k for k in c if k in OPS]
        if c.get("m") not in METRICS or len(ops) != 1 or not isinstance(c[ops[0]], (int, float)):
            errs.append(f"{where}: 조건 형식 오류 {c}")
    return errs


def validate_catalog(cat: dict) -> list[str]:
    """카탈로그 점검 — 오류 문장 목록(비었으면 정상). 방향과 임계값 순서가 맞는지도 본다:
    high_risk_off 면 위험회피 첫 조건은 이상(gte/gt)·위험선호 첫 조건은 이하(lte/lt)이고 같은 지표에서 회피 임계 > 선호 임계."""
    errs, seen = [], set()
    comp = cat.get("composite") or {}
    if not (isinstance(comp.get("risk_on"), (int, float)) and isinstance(comp.get("risk_off"), (int, float))
            and comp["risk_on"] > comp["risk_off"]):
        errs.append("composite: risk_on > risk_off 숫자여야 함")
    pts = comp.get("score")
    if not isinstance(pts, dict) or not all(isinstance(pts.get(st), (int, float)) and not isinstance(pts.get(st), bool)
                                            for st in STATES):
        errs.append(f"composite.score: {STATES} 모두 숫자여야 함")
    for f in ("stale_bdays", "stale_days_monthly"):
        if f in comp and not (isinstance(comp[f], int) and not isinstance(comp[f], bool) and comp[f] > 0):
            errs.append(f"composite.{f}: 양의 정수여야 함")
    for s in cat.get("signals") or []:
        k = s.get("key")
        if not k or k in seen:
            errs.append(f"키 없음·중복: {k}")
        seen.add(k)
        for f in ("name", "group", "status", "character", "grade_note"):
            if not s.get(f):
                errs.append(f"{k}: {f} 없음")
        if s.get("grade") not in ("A", "B", "C"):
            errs.append(f"{k}: grade 는 A·B·C")
        if s.get("direction") not in DIRECTIONS:
            errs.append(f"{k}: direction 은 {DIRECTIONS}")
        if not isinstance(s.get("in_composite"), bool):
            errs.append(f"{k}: in_composite 는 true/false")
        st = s.get("status")
        if st == PHASE2:
            if not s.get("candidates") or not s.get("definition"):
                errs.append(f"{k}: 원천 미확정 항목은 definition·candidates 필요")
            if s.get("rule") or s.get("input") or s.get("in_composite"):
                errs.append(f"{k}: 원천 미확정 항목은 rule·input 없이, 합성 제외")
            continue
        if st not in IMPLEMENTED:
            errs.append(f"{k}: status 는 {IMPLEMENTED + (PHASE2,)} 중 하나")
            continue
        for f in ("input", "unit", "source", "threshold_note"):
            if not s.get(f):
                errs.append(f"{k}: {f} 없음")
        if (s.get("transform") or {}).get("kind") not in KINDS:
            errs.append(f"{k}: transform.kind 는 {KINDS}")
        rule = s.get("rule") or {}
        e = _conds_ok(rule.get("risk_off"), f"{k}.risk_off") + _conds_ok(rule.get("risk_on"), f"{k}.risk_on")
        if "contrarian" in s:
            ct = s["contrarian"] if isinstance(s["contrarian"], dict) else {}
            e += _conds_ok(ct.get("when"), f"{k}.contrarian")
            if not isinstance(ct.get("note"), str) or not ct["note"].strip():
                e.append(f"{k}.contrarian: note 는 비어 있지 않은 문자열")
        errs += e
        if e or s.get("direction") not in DIRECTIONS:
            continue
        off, on = rule["risk_off"][0], rule["risk_on"][0]
        op_off = next(o for o in OPS if o in off)
        op_on = next(o for o in OPS if o in on)
        hi, lo = (off, on) if s["direction"] == "high_risk_off" else (on, off)
        op_hi, op_lo = (op_off, op_on) if s["direction"] == "high_risk_off" else (op_on, op_off)
        if off["m"] != on["m"]:
            errs.append(f"{k}: 위험회피·위험선호 첫 조건은 같은 지표여야 함")
        elif op_hi not in ("gte", "gt") or op_lo not in ("lte", "lt") or not hi[op_hi] > lo[op_lo]:
            errs.append(f"{k}: direction={s['direction']} 과 임계값 방향이 맞지 않음")
    return errs


def load_catalog(path=CATALOG) -> dict:
    cat = json.loads(path.read_text(encoding="utf-8"))
    errs = validate_catalog(cat)
    if errs:
        raise ValueError("universe/timing.json 오류 — " + "; ".join(errs))
    return cat


# ── 계산(네트워크 없음) ──
def _r(x, d: int = 2):
    """반올림 — NaN·inf·None 은 None(JSON allow_nan=False 안전)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, d) if math.isfinite(x) else None


def _clean(s: pd.Series | None) -> pd.Series:
    if s is None or not len(s):
        return pd.Series(dtype=float)
    s = pd.to_numeric(s, errors="coerce").replace([math.inf, -math.inf], math.nan).dropna()
    s.index = pd.DatetimeIndex(s.index)
    return s[~s.index.duplicated(keep="last")].sort_index()


def pct_rank(win: pd.Series, cur: float) -> float:
    """mid-rank 백분위 % — 창 안에서 cur 보다 낮은 개수 + 같은 개수 ½, ÷ 창 길이(valhist.position·analytics.stats 와 같음)."""
    return float(((win < cur).mean() + 0.5 * (win == cur).mean()) * 100)


def series_stats(s: pd.Series) -> dict | None:
    """value·as_of·pct_1y·pct_5y·z_1y·chg_20d — 창은 관측치 개수: 1년 = 최근 252개(다 차야 백분위 — spikes.pct_1y 와 같음),
    5년 = 최근 1260개(60개 초과면 계산, 모자라면 pct_5y_from 에 시작일). z 도 최근 252개(20개 초과)."""
    s = _clean(s)
    if s.empty:
        return None
    d0, v = s.index[-1], float(s.iloc[-1])
    w1, w5 = s.tail(WIN_1Y), s.tail(WIN_5Y)
    sd = float(w1.std()) if len(w1) > MIN_Z else 0.0
    return {"value": v, "as_of": d0.date().isoformat(),
            "pct_1y": pct_rank(w1, v) if len(w1) >= WIN_1Y else None,
            "pct_5y": pct_rank(w5, v) if len(w5) > MIN_PCT else None,
            "pct_5y_from": w5.index[0].date().isoformat() if MIN_PCT < len(w5) < WIN_5Y else None,
            "z_1y": (v - float(w1.mean())) / sd if sd > 0 else None,
            "chg_20d": v - float(s.iloc[-21]) if len(s) > 20 else None,
            "history_from": s.index[0].date().isoformat()}


def transform(spec: dict, series: list[pd.Series]) -> pd.Series:
    """카탈로그 transform 을 적용한 시계열(거래일 = 값이 있는 날 기준 위치 이동)."""
    kind = spec["kind"]
    s = _clean(series[0])
    if kind == "level":
        return s * spec.get("scale", 1)
    if kind == "ratio":
        b = _clean(series[1])
        df = pd.concat([s, b], axis=1, join="inner")
        df = df[df.iloc[:, 1] != 0]
        return df.iloc[:, 0] / df.iloc[:, 1]
    if kind == "ma_gap":
        return (s / s.rolling(spec["window"]).mean() - 1) * 100
    if kind == "ret":
        return (s.shift(spec.get("skip", 0)) / s.shift(spec["lag"]) - 1) * 100
    if kind == "chg_pct":
        return (s / s.shift(spec["lag"]) - 1) * 100
    raise ValueError(f"알 수 없는 transform {kind}")


def _check(conds: list, m: dict) -> bool | None:
    """조건 모두 만족? — 하나라도 거짓이면 False, 거짓은 없고 모르는 지표가 있으면 None, 모두 참이면 True."""
    unknown = False
    for c in conds:
        op = next(o for o in OPS if o in c)
        v = m.get(c["m"])
        if v is None:
            unknown = True
        elif not OPS[op](v, c[op]):
            return False
    return None if unknown else True


def judge(rule: dict, m: dict) -> str | None:
    """위험회피 → 위험선호 → 중립 순. 판정에 필요한 지표가 없어 결론이 갈릴 수 있으면 None."""
    off, on = _check(rule["risk_off"], m), _check(rule["risk_on"], m)
    if off:
        return "위험회피"
    if on:
        return "위험선호"
    if off is False and on is False:
        return "중립"
    return None


def _valhist_stats(item: dict | None, metric: str) -> dict | None:
    """밸류에이션 행 — valhist.position(최근 12·60개월) 그대로. z_1y 는 최근 12개월 값(6개 이상).
    최근 60개월 중 유효한 달이 60개 미만이면 pct_5y_from = 그 창의 첫 유효 달(5년 백분위가 더 짧은 창이라는 표시)."""
    import valhist   # refinitiv_src 를 끌어오므로 필요할 때만
    p1, p5 = valhist.position(item, metric, 12), valhist.position(item, metric, 60)
    if not p5:
        return None
    vals = (item or {}).get(metric) or []
    dates = (item or {}).get("dates") or []
    has_dates = len(dates) == len(vals)
    valid = [i for i, v in enumerate(vals) if v is not None and v > 0]
    win5 = [i for i in valid if i >= len(vals) - 60]
    last12 = [v for v in vals[-12:] if v is not None and v > 0]
    sd = float(pd.Series(last12).std()) if len(last12) >= 6 else 0.0
    cur = p5[0]
    return {"value": cur, "as_of": dates[valid[-1]] if valid and has_dates else None,
            "pct_1y": float(p1[1]) if p1 else None, "pct_5y": float(p5[1]),
            "pct_5y_from": dates[win5[0]] if has_dates and win5 and len(win5) < 60 else None,
            "z_1y": (cur - sum(last12) / len(last12)) / sd if sd > 0 else None, "chg_20d": None,
            "history_from": dates[valid[0]] if valid and len(dates) == len(vals) else None}


def _say(spec: dict, row: dict) -> str:
    if not row["ok"]:
        return f"{spec['name']}: 계산 못 함 — {row['reason']}."
    v, u = row["value"], spec["unit"]
    head = f"{spec['name']} {v:,}{'' if u in ('%', '배') else ' '}{u}"
    pos = []
    if row.get("pct_1y") is not None:
        pos.append(f"1년 중 {row['pct_1y']:.0f}% 위치")
    if row.get("pct_5y") is not None:
        pos.append(f"5년 중 {row['pct_5y']:.0f}%")
    if row.get("z_1y") is not None:
        pos.append(f"z {row['z_1y']:+.2f}")   # 판정과 같은 자릿수(소수 둘째) — 1.97 이 '+2.0' 으로 보이면 상태와 어긋남
    tail = f"{row['state']}" if row["judged"] else "판정 보류(이력 부족)"
    text = f"{head}" + (f" — {', '.join(pos)}" if pos else "") + f" → {tail}."
    if row.get("stale"):
        text += f" 기준일 {row['as_of']} 지연 — 합성 제외."
    if row.get("contrarian_note"):
        text += " " + row["contrarian_note"]
    return text


def _pick(spec: dict, inputs: dict) -> tuple[str | None, pd.Series | None, dict | None]:
    """입력 후보(예: kospi200 → kospi) 중 규칙이 쓰는 지표를 모두 계산할 만큼 이력이 있는 첫 후보 → (이름, 원 시계열, 통계).
    그런 후보가 없으면 변환값이라도 있는 첫 후보, 그것도 없으면 값이 있는 첫 후보(통계 None)."""
    need = {c["m"] for c in spec["rule"]["risk_off"] + spec["rule"]["risk_on"]}
    shown = present = None
    for n in spec["input"]:
        s = _clean(inputs.get(n))
        if s.empty:
            continue
        present = present or (n, s, None)
        st = series_stats(transform(spec["transform"], [s]))
        if st is None:
            continue
        if all(st[k] is not None for k in need):
            return n, s, st
        shown = shown or (n, s, st)
    return shown or present or (None, None, None)


def signal_row(spec: dict, inputs: dict, valhist_doc: dict | None = None) -> dict:
    """카탈로그 한 줄 → 시그널 행. 예외를 밖으로 내지 않는다(어떤 오류든 ok=false·reason '계산 오류: …').
    계약: ok = 값을 계산함(value·as_of 있음, 아니면 reason). judged = 상태(state)를 정함 — ok 이어도 판정 지표가 모자라면
    false 이고 화면은 '보류'. 판정·역발상 조건은 화면에 보이는 반올림 값으로 본다(보이는 숫자와 상태가 어긋나지 않게).
    stale(기준일 지연)은 여러 신호를 함께 봐야 하므로 compute() 가 붙인다."""
    base = {k: spec.get(k) for k in ("key", "name", "group", "unit", "source", "character", "direction",
                                     "grade", "grade_note", "threshold_note", "in_composite", "status")}
    empty = {**base, "ok": False, "judged": False, "stale": False, "reason": None, "value": None, "as_of": None,
             "input_used": None, "pct_1y": None, "pct_5y": None, "pct_5y_from": None, "z_1y": None, "chg_20d": None,
             "history_from": None, "state": None, "contrarian_note": None}
    row = dict(empty)

    def fail(reason: str) -> dict:
        row.update(reason=reason, say=_say(spec, row))
        return row

    try:
        tr = spec["transform"]
        if tr["kind"] == "valhist":
            items = (valhist_doc or {}).get("items") or {}
            name = next((n for n in spec["input"] if n.split(":", 1)[1] in items
                         and (items[n.split(":", 1)[1]].get(tr["metric"]) or [])), None)
            st = _valhist_stats(items[name.split(":", 1)[1]], tr["metric"]) if name else None
            if not st:
                return fail("valhist 이력 없음(data/equity_valhist.json)" if not name else "월말 값 6개월 미만")
            hist_from = st["history_from"]
        else:
            if tr["kind"] == "ratio":
                missing = [n for n in spec["input"] if not len(_clean(inputs.get(n)))]
                if missing:
                    return fail(f"입력 없음: {', '.join(missing)}")
                name, ss = "÷".join(spec["input"]), [_clean(inputs[n]) for n in spec["input"]]
                st = series_stats(transform(tr, ss))
            else:
                name, s, st = _pick(spec, inputs)
                if name is None:
                    return fail(f"입력 없음: {' 또는 '.join(spec['input'])}")
                ss = [s]
            if not st:
                return fail("이력이 짧아 변환값 없음")
            hist_from = max(x.index[0] for x in ss).date().isoformat()   # 변환 전 원 입력의 시작(비율은 둘 다 있는 첫날)
        d = 1 if spec["unit"] == "bp" else 3 if spec["unit"] == "배" and tr["kind"] == "ratio" else 2
        shown = {"value": _r(st["value"], d), "pct_1y": _r(st["pct_1y"], 0), "pct_5y": _r(st["pct_5y"], 0),
                 "z_1y": _r(st["z_1y"], 2), "chg_20d": _r(st["chg_20d"], d)}
        state = judge(spec["rule"], shown)
        row.update(ok=True, judged=state is not None, state=state, input_used=name, as_of=st["as_of"],
                   history_from=hist_from, pct_5y_from=st["pct_5y_from"], **shown)
        if spec.get("contrarian") and _check(spec["contrarian"]["when"], shown):
            row["contrarian_note"] = spec["contrarian"]["note"]
        if spec["key"] == "trend_200d" and tr["kind"] == "ma_gap":
            gap = _clean(transform(tr, ss)).tail(22)    # 최근 21거래일 각 날 vs 전날 → 22개
            row["flips_1m"] = int((gap.gt(0).astype(int).diff().abs() > 0).sum()) if len(gap) > 1 else None
        if state is None:
            row["reason"] = "판정 지표 부족(이력 짧음)"
        row["say"] = _say(spec, row)
        return row
    except Exception as e:  # noqa: BLE001 — 한 신호 실패가 표 전체를 막지 않게
        row = {**empty, "reason": f"계산 오류: {type(e).__name__}: {e}"}
        row["say"] = f"{spec.get('name') or spec.get('key')}: 계산 못 함 — {row['reason']}."
        return row


def mark_stale(rows: list[dict], cat: dict, specs: list[dict] | None = None) -> None:
    """기준일 지연 표시(제자리 수정) — 기준 = ok 인 일별 행(YYYY-MM-DD) 기준일 중 가장 최근.
    일별 행은 영업일 차 > composite.stale_bdays(기본 5), 월말 행(YYYY-MM, valhist)은 그 달 말일과의 달력일 차 >
    composite.stale_days_monthly(기본 45) 면 stale=true·reason. 값·상태는 그대로 보여 주고 합성에서만 뺀다."""
    cfg = cat.get("composite") or {}
    nb, nm = cfg.get("stale_bdays", STALE_BDAYS), cfg.get("stale_days_monthly", STALE_DAYS_MONTHLY)
    daily = [r["as_of"] for r in rows if r["ok"] and len(r.get("as_of") or "") == 10]
    if not daily:
        return
    ref = max(daily)
    for i, r in enumerate(rows):
        a = r.get("as_of") or ""
        if not r["ok"] or len(a) not in (7, 10):
            continue
        if len(a) == 10:
            lag = len(pd.bdate_range(a, ref)) - 1 if a < ref else 0
            late, what = lag > nb, f"{lag}영업일"
        else:
            lag = (pd.Timestamp(ref) - pd.Period(a, "M").end_time.normalize()).days
            late, what = lag > nm, f"{lag}일"
        if late:
            msg = f"기준일 지연: {a} — 최신 {ref} 보다 {what} 늦음(합성 제외)"
            r.update(stale=True, reason=f"{r['reason']}; {msg}" if r["reason"] else msg)
            r["say"] = _say(specs[i] if specs else r, r)


def composite(rows: list[dict], cat: dict) -> dict:
    """동일가중 합성 — 상태 점수를 group 안에서 평균한 뒤 group 평균. 쓸 수 있는 신호가 없으면 score None.
    분모(n_total·n_groups_total) = in_composite 이고 '구현·데이터 대기'가 아닌 신호 — 데이터 대기는 waiting 으로만 나열.
    판정 못 한 신호는 missing, 기준일 지연(stale)은 stale 로 나열하고 둘 다 점수에서 뺀다.
    라벨은 표시 점수(소수 둘째 자리)에 엄격 부등호: > risk_on 위험선호, < risk_off 위험회피(정확히 1/3 = 0.33 은 중립)."""
    cfg = cat["composite"]
    pts = cfg["score"]
    comp = [r for r in rows if r.get("in_composite")]
    waiting = [r["key"] for r in comp if r.get("status") == WAITING]
    eligible = [r for r in comp if r.get("status") != WAITING]
    used = [r for r in eligible if r["ok"] and r["state"] in pts and not r.get("stale")]
    used_keys = {u["key"] for u in used}
    groups = {}
    for r in used:
        groups.setdefault(r["group"], []).append(pts[r["state"]])
    gs = {g: sum(v) / len(v) for g, v in groups.items()}
    score = _r(sum(gs.values()) / len(gs), 2) if gs else None
    label = None if score is None else ("위험선호" if score > cfg["risk_on"] else "위험회피" if score < cfg["risk_off"] else "중립")
    return {"score": score, "label": label, "n_used": len(used), "n_total": len(eligible),
            "n_groups": len(gs), "n_groups_total": len({r["group"] for r in eligible}),
            "groups": {g: _r(v, 2) for g, v in gs.items()},
            "missing": [r["key"] for r in eligible if r["key"] not in used_keys and not r.get("stale")],
            "stale": [r["key"] for r in eligible if r.get("stale")],
            "waiting": waiting,
            "thresholds": {"risk_on": cfg["risk_on"], "risk_off": cfg["risk_off"]}, "note": cfg.get("note")}


def compute(inputs: dict, cat: dict, valhist_doc: dict | None = None, as_of: dict | None = None,
            is_sample: bool = False, errors: list | None = None, now: datetime.datetime | None = None) -> dict:
    """순수 계산 — inputs = {이름: pd.Series(날짜 인덱스)}. 테스트는 합성 시계열로 이것만 부른다."""
    now = now or datetime.datetime.now()
    impl = [s for s in cat["signals"] if s["status"] in IMPLEMENTED]
    rows = [signal_row(s, inputs, valhist_doc) for s in impl]
    mark_stale(rows, cat, impl)
    gen = now.isoformat(timespec="seconds")
    return {"generated_at": gen,
            "meta": {"generated_at": gen, "as_of": as_of or {}, "is_sample": bool(is_sample), "errors": errors or [],
                     "catalog_version": (cat.get("meta") or {}).get("version"),
                     "n_ok": sum(r["ok"] for r in rows), "n_judged": sum(r["judged"] for r in rows),
                     "n_signals": len(rows)},
            "method": METHOD,
            "composite": composite(rows, cat),
            "signals": rows,
            "phase2": [{k: s.get(k) for k in ("key", "name", "status", "definition", "candidates", "grade", "grade_note")}
                       for s in cat["signals"] if s["status"] == PHASE2],
            "notes": ["판정 기준·근거 등급은 universe/timing.json, 백분위는 mid-rank(자기 과거 분포 안 위치, 1년 = 최근 252개·5년 = 최근 1260개).",
                      "ok = 값 계산됨, judged = 상태 판정됨(false 면 '보류'), stale = 기준일 지연으로 합성 제외.",
                      "역발상 신호(VKOSPI·VIX)는 높을 때 '위험회피' 국면이지만, 극단이면 역발상 매수 참고 문구를 붙입니다.",
                      "VIX·VIX3M 은 yfinance(Refinitiv 권한 없음, 예외 원천). 그 밖은 Refinitiv.",
                      "출처: Refinitiv(LSEG)·yfinance · 로컬 전용(재배포 금지) · 매매 지시 아님."]}


# ── 읽기(저장소 안 파일만, 네트워크 없음) ──
def load_inputs() -> tuple[dict, dict | None, dict, list]:
    """(inputs, valhist 문서, 원천별 기준일, 오류) — 원천 하나가 없어도 나머지로 계속한다."""
    from agent import load_json
    inputs, as_of, errors = {}, {}, []
    try:
        import analytics
        p = analytics.load_panel()
        for c in PANEL_COLS:
            if c in p and p[c].notna().any():
                inputs[c] = p[c].dropna()
        last = [inputs[c].index[-1] for c in inputs]
        as_of["analytics_panel"] = max(last).date().isoformat() if last else None
    except Exception as e:  # noqa: BLE001 — data/history 가 없으면 RuntimeError
        errors.append(f"analytics 패널: {e}")
    frames = []   # equity_daily(equity.py) 먼저, timing_daily(spikes.py — ^VIX3M 은 여기에만) — 같은 날은 앞 파일 값
    for path in (EQ_HIST, TIMING_HIST):
        try:
            if path.exists():
                f = pd.read_csv(path, index_col=0, parse_dates=True)
                frames.append(f)
                as_of[path.stem] = f.index.max().date().isoformat() if len(f) else None
            elif path == EQ_HIST:
                errors.append(f"{EQ_HIST.name} 없음 — python equity.py")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path.stem}: {e}")
    try:
        for col, name in EQ_COLS.items():
            parts = [f[col].dropna() for f in frames if col in f and f[col].notna().any()]
            if parts and name not in inputs:
                s = parts[0]
                for x in parts[1:]:
                    s = s.combine_first(x)
                inputs[name] = s
    except Exception as e:  # noqa: BLE001
        errors.append(f"equity·timing_daily 합치기: {e}")
    m = load_json(config.DATA_DIR / "market.json") or {}
    f = (m.get("equity") or {}).get("vkospi")
    if isinstance(f, dict) and f.get("value") is not None and f.get("as_of"):
        d = pd.Timestamp(f["as_of"])
        d = (d.tz_localize(None) if d.tzinfo else d).normalize()   # 시각이 붙은 기준일도 '날짜'로 비교(같은 날 중복 방지)
        s = inputs.get("vkospi", pd.Series(dtype=float))
        if not len(s) or d > s.index[-1]:
            inputs["vkospi"] = pd.concat([s, pd.Series([float(f["value"])], index=[d])])
        as_of["market_json"] = (m.get("meta") or {}).get("generated_at")
    import valhist
    vh = valhist.load()
    if vh:
        as_of["valhist"] = vh.get("generated_at")
    else:
        errors.append("equity_valhist.json 없음 — python valhist.py")
    return inputs, vh, as_of, errors


def build(save: bool = True) -> dict:
    cat = load_catalog()
    inputs, vh, as_of, errors = load_inputs()
    d = compute(inputs, cat, vh, as_of=as_of, is_sample=False, errors=errors)   # 예시(sample) 파일은 읽지 않는다
    if save:
        if not d["meta"]["n_ok"]:
            raise RuntimeError("시그널 입력이 하나도 없음 — 기존 timing.json 유지: " + "; ".join(errors))
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
    if not d:
        return None
    return (datetime.datetime.now() - datetime.datetime.fromisoformat(d["generated_at"])).total_seconds() / 3600


def to_markdown(d: dict) -> str:
    """에이전트 컨텍스트 — 신호별 한 줄과 합성 점수."""
    c = d["composite"]
    lines = [f"## 마켓타이밍 시그널 ({d['generated_at'][:16]})",
             f"합성 {c['label'] or '-'} (점수 {c['score'] if c['score'] is not None else '-'}, 신호 {c['n_used']}/{c['n_total']}·묶음 {c['n_groups']}/{c['n_groups_total']}, 동일가중)",
             "| 신호 | 값 | 1년 백분위 | 5년 백분위 | z(1년) | 상태 | 등급 | 기준일 |", "|---|---|---|---|---|---|---|---|"]
    n = lambda v: "-" if v is None else v  # noqa: E731
    for r in d["signals"]:
        lines.append(f"| {r['name']} | {n(r['value'])}{r['unit'] if r['value'] is not None else ''} | {n(r['pct_1y'])} | {n(r['pct_5y'])} | "
                     f"{n(r['z_1y'])} | {(r['state'] if r['judged'] else '계산 못 함' if not r['ok'] else '보류') + (' (지연·합성 제외)' if r.get('stale') else '')} | {r['grade']} | {n(r['as_of'])} |")
    lines += [f"- {t}" for t in d.get("notes", [])]
    return "\n".join(lines)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = build()
    for r in d["signals"]:
        print(r["say"])
    c = d["composite"]
    print(f"합성: {c['label']} (점수 {c['score']}, 신호 {c['n_used']}/{c['n_total']}) · {OUT}")
