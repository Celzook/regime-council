# -*- coding: utf-8 -*-
"""투자 포인트 점검 보고서 — 4개 점검 항목(손실한도·과거 사례·펀더멘탈·MP 괴리) + 조정안 + 자체검증.

코드가 계산: 한도 현황(현재·pro-forma), 과거 유사 국면, MP 괴리, 리스크 등급 하한, 두 패스 비교.
에이전트가 작성: 해석·조정안·운용역 확인 항목. API 키가 없으면 CODE 부분만 담긴 보고서를 만든다.
"""
import json
import os

import anthropic

import config
import history
import limits
import store
from agent import Agent, build_shared_context, load_json, load_prompt
from schemas import CheckReport, VerifierView

TRIGGERS = {"NEW": "신규 투자", "REBAL_6M": "6개월 주기 비중 조절",
            "LIMIT_APPROACH": "손실한도 접근", "SHOCK": "시장 급변"}
GRADE_ORDER = {"NONE": 0, "WATCH": 1, "WARN": 2, "REDUCE": 3, "HALT": 4}
GRADE_KO = {"NONE": "없음", "WATCH": "관찰", "WARN": "경고", "REDUCE": "축소", "HALT": "중단"}
ASSET_KO = {"bond": "채권", "equity": "주식", "fx": "환"}
NEW_FIELDS = ("name", "asset", "mv_bn", "duration", "spread_duration", "spread_beta", "beta", "currency",
              "fx_exposure_bn", "hedge_pct", "stop_loss_pct", "fx_stop_pct", "rate_pnl_in_scope", "rating")


ALLOWED_INPUT_KEYS = NEW_FIELDS + ("carry_bp", "deadline", "bucket", "note", "shock_detail", "alert_detail")
OPPOSITE = {("INCREASE", "REDUCE"), ("REDUCE", "INCREASE"), ("HEDGE_UP", "HEDGE_DOWN"), ("HEDGE_DOWN", "HEDGE_UP")}


def sanitize_inputs(inputs: dict | None) -> dict:
    """운용역 입력 화이트리스트 + 길이 제한 (프롬프트·보고서 파일로 흘러가므로)."""
    out = {}
    for k, v in (inputs or {}).items():
        if k not in ALLOWED_INPUT_KEYS:
            continue
        if isinstance(v, str):
            v = v.strip()[:300]
            if not v:
                continue
        out[k] = v
    return out


def _floor_from(ev: dict, asset: str) -> str:
    fl = ev.get("floor_by_asset", {}).get(asset, ev["risk_floor"])
    return "NONE" if fl == "NORMAL" else fl


def _max_grade(*gs):
    return max(gs, key=lambda g: GRADE_ORDER[g])


def gap_md(rows: list[dict]) -> str:
    if not rows:
        return "### MP 괴리\n(채택된 MP 없음 — MP 를 먼저 산출·채택하면 괴리 분석이 가능)"
    L = ["### MP 대비 괴리 (CODE 계산, 전체 포트 대비 비중)",
         "| position_id | 버킷 | 현 비중 | MP 목표 | 괴리(%p) | 밴드 | 듀레이션 현/목표 | 헤지 현/목표 | 상태 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['position_id']} | {r['bucket']} | {r['now_pct']} | {r['mp_pct'] if r['mp_pct'] is not None else '-'} | {r['gap_pp'] if r['gap_pp'] is not None else '-'} | ±{r['band_pp']} | "
                 f"{r['duration_now'] or '-'}/{r['duration_mp'] or '-'} | {r['hedge_now_pct'] if r['hedge_now_pct'] is not None else '-'}/{r['hedge_mp_pct'] if r['hedge_mp_pct'] is not None else '-'} | {r['status']} |")
    return "\n".join(L)


def macro_md() -> str:
    manual = load_json(config.DATA_DIR / "manual_inputs.json") or {}
    keys = {k: v for k, v in manual.items() if isinstance(v, dict) and "value" in v}
    if not keys:
        return "### 거시 지표 (수동 입력)\n(없음 — 성장·물가·정책 지표는 data/manual_inputs.json 에 입력하면 펀더멘탈 점검에 반영)"
    return "### 거시 지표 (수동 입력)\n" + "\n".join(f"- {k}: {v['value']}{v.get('unit','')} ({v.get('as_of')}, {v.get('source')})" for k, v in keys.items())


def compare_checks(p: dict | None, v: dict | None, code_floor: str = "NONE") -> dict:
    """산출자·검증자 비교 — 등급은 둘 다 코드 하한을 적용한 뒤 비교한다."""
    if not p or not v:
        return {"verdict": "NOT_RUN", "rule": "자체검증 미실행 — 운용역 검토", "details": {}}
    pg, vg = _max_grade(code_floor, p["risk_grade"]), _max_grade(code_floor, v["risk_grade"])
    gdiff = abs(GRADE_ORDER[pg] - GRADE_ORDER[vg])
    pa = {a["position_id"]: a for a in p.get("adjustments", [])}
    va = {a["position_id"]: a for a in v.get("adjustments", [])}
    dir_mismatch, opposite, max_w = 0, 0, 0.0
    for pid in set(pa) & set(va):
        d1, d2 = pa[pid]["direction"], va[pid]["direction"]
        if d1 != d2:
            dir_mismatch += 1
            if (d1, d2) in OPPOSITE:
                opposite += 1
        if pa[pid].get("weight_proposed_pct") is not None and va[pid].get("weight_proposed_pct") is not None:
            max_w = max(max_w, abs(pa[pid]["weight_proposed_pct"] - va[pid]["weight_proposed_pct"]))
    only_one = len(set(pa) ^ set(va))
    if opposite:
        verdict = "DISAGREE"
    elif gdiff == 0 and dir_mismatch == 0 and max_w <= 2 and only_one == 0:
        verdict = "AGREE"
    elif gdiff <= 1 and dir_mismatch <= 1 and max_w <= 5 and only_one <= 1:
        verdict = "PARTIAL"
    else:
        verdict = "DISAGREE"
    return {"verdict": verdict, "producer_grade": pg, "verifier_grade": vg, "producer_grade_raw": p["risk_grade"],
            "verifier_grade_raw": v["risk_grade"], "direction_mismatch": dir_mismatch, "opposite_directions": opposite,
            "max_weight_pp": round(max_w, 2), "unmatched_positions": only_one,
            "details": {"rule": "일치: 등급 동일·방향 동일·비중 차≤2%p·미매칭 0 / 부분: 등급 1단계·방향 불일치 1건·비중 차≤5%p·미매칭≤1 / 정반대 방향(확대 vs 축소)은 즉시 불일치"}}


def decision_rule(verdict: str, limit_breach: bool, final_grade: str) -> str:
    if limit_breach or final_grade == "HALT":
        return "즉시 축소·보고 (한도 초과 신호 — 코드 판정)"
    if verdict == "AGREE":
        return "운용역 검토 후 실행"
    if verdict == "NOT_RUN":
        return "자체검증 미실행 — 운용역 검토"
    if verdict == "PARTIAL":
        return "부분 불일치 — 차이 항목 추가 분석 후 재산출"
    return "불일치 — 보류, 추가 분석 후 재점검"


def run_check(trigger: str, asset: str, inputs: dict | None = None, model: str | None = None,
              operator_note: str = "") -> dict:
    inputs = sanitize_inputs(inputs)
    operator_note = (operator_note or "")[:500]
    if trigger not in TRIGGERS or asset not in ASSET_KO:
        raise ValueError("trigger/asset 값이 잘못됨")
    ev_now = limits.evaluate()
    new_pos = None
    if trigger == "NEW":
        new_pos = {k: inputs[k] for k in NEW_FIELDS if inputs.get(k) not in (None, "")}
        for k in ("mv_bn", "duration", "spread_duration", "spread_beta", "beta", "fx_exposure_bn", "hedge_pct", "stop_loss_pct", "fx_stop_pct"):
            if k in new_pos:
                new_pos[k] = float(new_pos[k])
        new_pos.setdefault("asset", asset if asset != "fx" else "bond")
        if new_pos.get("currency") and "fx_exposure_bn" not in new_pos:
            new_pos["fx_exposure_bn"] = new_pos.get("mv_bn", 0)
    ev = limits.evaluate(new_position=new_pos) if new_pos else ev_now

    analog, analog_md = None, ""
    try:
        analog = history.find_analogs(asset)
        analog_md = history.to_markdown(analog)
    except Exception as e:  # noqa: BLE001
        analog_md = f"### 과거 유사 국면\n(계산 불가: {e})"

    mp = store.load_mp(asset, adopted_only=True) or store.load_mp(asset)
    gaps = limits.gap_analysis(ev_now, {asset: mp}, band_pp=limits.load_limits().get("mp_band_pp", 2.0)) if mp else []

    trig_md = [f"### 투자 포인트: {TRIGGERS[trigger]} ({trigger}) · 담당 {ASSET_KO[asset]} 에이전트"]
    if new_pos:
        trig_md.append("신규 포지션(USER 입력): " + json.dumps(new_pos, ensure_ascii=False))
        if inputs.get("carry_bp"):
            trig_md.append(f"예상 캐리(USER): {inputs['carry_bp']}bp")
        if inputs.get("deadline"):
            trig_md.append(f"집행 기한(USER): {inputs['deadline']}")
    for k in ("bucket", "note", "shock_detail", "alert_detail"):
        if inputs.get(k):
            trig_md.append(f"{k}(USER): {inputs[k]}")
    if operator_note:
        trig_md.append(f"운용역 메모: {operator_note}")
    code_floor = _floor_from(ev, asset)
    breach = ev["breach_by_asset"].get(asset, ev["limit_breach"])
    if new_pos and new_pos.get("currency") not in (None, "KRW"):
        breach = breach or ev["breach_by_asset"].get("fx", False)
    context = "\n\n".join(["\n".join(trig_md),
                           ("#### 신규 포지션 반영 후 (pro-forma)\n" if new_pos else "") + limits.to_markdown(ev),
                           ("#### 신규 포지션 반영 전 (현재)\n" + limits.to_markdown(ev_now)) if new_pos else "",
                           analog_md, gap_md(gaps),
                           (f"### 현재 MP ({mp['mp_id']}, {mp['status']})\n```json\n{json.dumps(mp['targets'], ensure_ascii=False)}\n```" if mp else ""),
                           macro_md(),
                           f"### 리스크 등급 하한 (CODE, {ASSET_KO[asset]} 기준): {code_floor} — 이 아래로 내릴 수 없음. 한도 초과(축소 이상): {'예' if breach else '아니오'}"])

    producer = verifier = None
    error = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            client = anthropic.Anthropic()
            shared = build_shared_context()
            producer = Agent(asset, shared, client, model=model).opinion(
                load_prompt("check_producer") + "\n\n# 입력\n\n" + context, CheckReport, mode="discuss")
            verifier = Agent(asset, shared, client, model=model).opinion(
                load_prompt("check_verifier") + "\n\n# 입력\n\n" + context, VerifierView, mode="discuss")
        except Exception as e:  # noqa: BLE001
            error = str(e)
    else:
        error = "ANTHROPIC_API_KEY 미설정 — CODE 계산 부분만 생성"

    p = producer.model_dump() if producer else None
    v = verifier.model_dump() if verifier else None
    final_grade = _max_grade(code_floor, *(g for g in ((p or {}).get("risk_grade"), (v or {}).get("risk_grade")) if g))
    comparison = compare_checks(p, v, code_floor)
    rule = decision_rule(comparison["verdict"], breach, final_grade)

    doc = {
        "report_id": store.new_id("CHK", store.CHECK_DIR), "trigger": trigger, "trigger_name": TRIGGERS[trigger],
        "asset": asset, "asset_name": ASSET_KO[asset], "inputs": inputs, "operator_note": operator_note,
        "created_at": store.now_iso(), "data_as_of": ev["as_of"], "market_as_of": None,
        "headline": p["headline"] if p else "(에이전트 미실행 — CODE 계산 결과만)",
        "code": {"limits": ev, "limits_before": ev_now if new_pos else None, "analog": analog, "gaps": gaps,
                 "mp_id": mp["mp_id"] if mp else None, "code_floor": code_floor},
        "producer": p, "verifier": v, "comparison": comparison,
        "final_grade": final_grade, "final_grade_ko": GRADE_KO[final_grade], "limit_breach": breach,
        "verdict": comparison["verdict"], "rule": rule,
        "slots": {"claude_producer": "DONE" if p else "NOT_RUN", "claude_verifier": "DONE" if v else "NOT_RUN", "chatgpt": "NOT_CONNECTED"},
        "decision": {"status": "PENDING", "by": None, "at": None, "reason": None},
        "error": error,
        "meta": store.meta_block("producer+verifier" if p else "code-only", {"context": context}, model),
    }
    store.save_check(doc)
    store.append_log("CHECK_CREATED", {"report_id": doc["report_id"], "trigger": trigger, "asset": asset,
                                       "final_grade": final_grade, "verdict": comparison["verdict"], "rule": rule,
                                       "prompt_version": doc["meta"]["prompt_version"], "model_id": doc["meta"]["model_id"]})
    (config.REPORT_DIR / f"{doc['report_id']}_점검보고서.md").write_text(to_markdown(doc), encoding="utf-8")
    return doc


def decide(report_id: str, status: str, by: str, reason: str = "") -> dict:
    doc = store.load_check(report_id)
    if not doc:
        raise KeyError(report_id)
    if status not in ("ADOPTED", "MODIFIED", "REJECTED", "HOLD"):
        raise ValueError("status")
    if status in ("ADOPTED", "MODIFIED") and doc["verdict"] in ("PARTIAL", "DISAGREE") and not reason:
        raise ValueError("불일치 보고서를 채택하려면 사유 입력이 필요합니다")
    if status == "ADOPTED" and doc.get("limit_breach"):
        incr = [a for a in (doc.get("producer") or {}).get("adjustments", []) if a.get("direction") in ("INCREASE", "HEDGE_DOWN")]
        if incr:
            raise ValueError("한도 초과 신호 상태에서는 확대·헤지 축소 방향 조정안을 채택할 수 없습니다 (수정 채택 또는 기각)")
    doc["decision"] = {"status": status, "by": by, "at": store.now_iso(), "reason": reason}
    store.save_check(doc)
    store.append_log("CHECK_DECISION", {"report_id": report_id, "status": status, "by": by, "reason": reason})
    return doc


def to_markdown(doc: dict) -> str:
    ev, p, v, c = doc["code"]["limits"], doc.get("producer"), doc.get("verifier"), doc["comparison"]
    L = [f"# 투자 포인트 점검 보고서 — {doc['report_id']}",
         f"트리거 {doc['trigger_name']} · 담당 {doc['asset_name']} 에이전트 · 생성 {doc['created_at']} · 포지션 기준일 {doc['data_as_of']}",
         f"모델 {doc['meta']['model_id']} · 프롬프트 v{doc['meta']['prompt_version']} · 입력 해시 {doc['meta']['inputs_hash']}", "",
         f"## 결론\n**{doc['headline']}**", "",
         f"- 리스크 등급: **{doc['final_grade_ko']} ({doc['final_grade']})** — 코드 하한 {doc['code']['code_floor']}" + (f", 산출자 {p['risk_grade']}" if p else ""),
         f"- 자체검증: **{c['verdict']}** (산출자 {c.get('producer_grade','-')} / 검증자 {c.get('verifier_grade','-')}, 비중 차 최대 {c.get('max_weight_pp','-')}%p) · ChatGPT 슬롯 미연결",
         f"- 의사결정 규칙: **{doc['rule']}**", ""]
    if doc.get("error"):
        L += [f"> 에이전트 실행 안내: {doc['error']}", ""]
    L += ["## 1. 손실한도 점검 (CODE)", limits.to_markdown(ev)]
    if ev.get("new_position"):
        L += ["", f"신규 포지션 추가 전 전체 소진율 {doc['code']['limits_before']['total']['util_pct']}% → 추가 후 {ev['total']['util_pct']}% (손익 0 가정, 스트레스는 위 표)"]
    if p:
        L += ["", f"**해석(AGENT)**: {p['limit_check']['conclusion']}"] + [f"- [{n['tag']}] {n['text']}" for n in p["limit_check"]["notes"]]
    L += ["", "## 2. 과거 사례 점검"]
    L.append(history.to_markdown(doc["code"]["analog"]) if doc["code"].get("analog") else "(과거 유사 국면 계산 불가)")
    if p:
        L += ["", "**해석(AGENT)**"] + [f"- {h['date']}: {h['then']} / 현재와 차이: {h['difference_now']}" for h in p["history_check"]["cases"]]
        L += [f"- 종합: {p['history_check']['overall']}", f"- 유의: {p['history_check']['caveat']}"]
    L += ["", "## 3. 펀더멘탈 점검"]
    if p:
        L += ["| 축 | 현재 [F] | MP 가정 [A] | 정합 | 비고 |", "|---|---|---|---|---|"]
        for a in p["fundamental_check"]["axes"]:
            L.append(f"| {a['axis']} | {a['now']} | {a['mp_assumption']} | {'○' if a['consistent'] else ('×' if a['consistent'] is False else '데이터 없음')} | {a['note']} |")
        L.append(f"\n{p['fundamental_check']['conclusion']}")
    else:
        L.append("(에이전트 미실행)")
    L += ["", "## 4. MP 대비 괴리 분석", gap_md(doc["code"]["gaps"])]
    if p:
        L += ["", f"**해석(AGENT)**: {p['gap_comment']}"]
    L += ["", "## 5. 조정안"]
    for src, obj in (("산출자", p), ("검증자", v)):
        if not obj:
            continue
        L += [f"**{src}**", "| position_id | 버킷 | 방향 | 비중 현→제안(%) | 듀레이션 현→제안 | 헤지 현→제안(%) | 금액(억) | 기한 | 근거 |", "|---|---|---|---|---|---|---|---|---|"]
        for a in obj["adjustments"]:
            rat = "; ".join(f"[{r['tag']}] {r['text']}" for r in a["rationale"])
            L.append(f"| {a['position_id']} | {a['bucket']} | {a['direction']} | {a.get('weight_now_pct','-')}→{a.get('weight_proposed_pct','-')} | {a.get('duration_now','-')}→{a.get('duration_proposed','-')} | {a.get('hedge_now_pct','-')}→{a.get('hedge_proposed_pct','-')} | {a.get('amount_bn','-')} | {a.get('deadline','')} | {rat} |")
        L.append("")
    if v:
        L += ["검증자가 본 핵심 리스크:"] + [f"- {k}" for k in v["key_risks"]] + ([f"- 비고: {v['note']}"] if v.get("note") else [])
    L += ["", "## 6. 운용역 확인 항목"]
    L += [f"- [ ] {o}" for o in (p["operator_checks"] if p else ["포지션·손익 수치 원장 대조", "한도표(가안) 수치 확인", "에이전트 미실행 — 키 등록 후 재점검"])]
    if p and p.get("data_gaps"):
        L += ["", "데이터 공백: " + "; ".join(p["data_gaps"])]
    d = doc["decision"]
    L += ["", f"## 7. 결정\n상태 {d['status']}" + (f" · {d['by']} · {d['at']} · 사유: {d['reason']}" if d.get("by") else ""),
          "", "---", "본 보고서는 AI 산출물(의사결정 지원 자료)로 운용역 결정·승인 전 효력이 없습니다. 한도·소진율·스트레스·괴리 수치는 코드 계산(CODE), 해석·조정안은 Claude 생성(AGENT)."]
    return "\n".join(L)
