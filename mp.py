# -*- coding: utf-8 -*-
"""모델 포트폴리오(MP) 산출 — 산출자 패스 + 독립 검증자 패스, 코드 비교.

ChatGPT 교차검증 슬롯은 비워 둔다(IT·준법 협의 후 연결). 그 전까지는
'자체검증(동일 모델 2패스)' — 검증자는 산출자 결과를 보지 않고 동일 입력으로 재산출한다.
"""
import datetime
import json

import anthropic

import config
import history
import limits
import store
from agent import Agent, build_shared_context, load_prompt
from schemas import ModelPortfolio

ASSET_KO = {"bond": "채권", "equity": "주식", "fx": "환"}


def asset_positions_md(ev: dict, asset: str) -> str:
    rows = []
    for r in ev["positions"]:
        if r["is_new"]:
            continue
        if asset == "bond" and r["asset"] in ("bond", "mixed"):
            rows.append(r)
        elif asset == "equity" and r["asset"] in ("equity", "mixed"):
            rows.append(r)
        elif asset == "fx" and r["fx"]:
            rows.append(r)
    L = [f"### 담당 자산({ASSET_KO[asset]}) 포지션 — target 은 아래 position_id 전부에 대해 작성",
         "| position_id | 이름 | 비중(%) | 듀레이션 | 베타 | 헤지비율(%) | 환오픈(억) |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['id']} | {r['name']} | {r['weight_pct']} | {r.get('duration') or '-'} | {r.get('beta') or '-'} | "
                 f"{r.get('hedge_pct') if r.get('hedge_pct') is not None else '-'} | {r['fx']['open_bn'] if r['fx'] else '-'} |")
    return "\n".join(L)


def build_context(asset: str, ev: dict | None = None) -> tuple[str, dict]:
    ev = ev or limits.evaluate()
    parts = [limits.to_markdown(ev)]
    analog = None
    try:
        analog = history.find_analogs(asset)
        parts.append(history.to_markdown(analog))
    except Exception as e:  # noqa: BLE001 — 과거 데이터 없으면 그 사실만 전달
        parts.append(f"### 과거 유사 국면\n(계산 불가: {e} — fetch_history.py 실행 필요)")
    current = store.load_mp(asset)
    if current:
        parts.append(f"### 현재 유효 MP ({current['mp_id']}, {current['status']}, 기준일 {current.get('data_as_of')})\n```json\n"
                     + json.dumps(current["producer"], ensure_ascii=False, indent=1) + "\n```")
    else:
        parts.append("### 현재 유효 MP\n(없음 — 최초 산출)")
    parts.append(asset_positions_md(ev, asset))
    return "\n\n".join(parts), {"analog_flags": analog["flags"] if analog else None,
                                "current_mp_id": current["mp_id"] if current else None}


def compare_mp(a: dict | None, b: dict | None) -> dict:
    if not a or not b:
        return {"verdict": "NOT_RUN", "rule": "자체검증 미실행 — 운용역 검토", "diffs": []}
    ta = {t["position_id"]: t for t in a.get("targets", [])}
    tb = {t["position_id"]: t for t in b.get("targets", [])}
    diffs, max_w, max_d, max_h, missing = [], 0.0, 0.0, 0.0, 0
    for pid in sorted(set(ta) | set(tb)):
        x, y = ta.get(pid), tb.get(pid)
        if not x or not y:
            missing += 1
            diffs.append({"position_id": pid, "missing_in": "verifier" if not y else "producer"})
            continue
        row = {"position_id": pid, "bucket": x.get("bucket")}
        if x.get("weight_pct") is not None and y.get("weight_pct") is not None:
            row["weight_pp"] = round(abs(x["weight_pct"] - y["weight_pct"]), 2); max_w = max(max_w, row["weight_pp"])
            row["weight_producer"], row["weight_verifier"] = x["weight_pct"], y["weight_pct"]
        if x.get("duration") is not None and y.get("duration") is not None:
            row["duration_diff"] = round(abs(x["duration"] - y["duration"]), 2); max_d = max(max_d, row["duration_diff"])
        if x.get("hedge_pct") is not None and y.get("hedge_pct") is not None:
            row["hedge_pp"] = round(abs(x["hedge_pct"] - y["hedge_pct"]), 1); max_h = max(max_h, row["hedge_pp"])
        diffs.append(row)
    if missing == 0 and max_w <= 2 and max_d <= 0.5 and max_h <= 5:
        verdict, rule = "AGREE", "일치 — 운용역 검토 후 채택 가능"
    elif missing <= 1 and max_w <= 5 and max_d <= 1.0 and max_h <= 10:
        verdict, rule = "PARTIAL", "부분 불일치 — 차이 항목 추가 분석 후 재산출"
    else:
        verdict, rule = "DISAGREE", "불일치 — 보류, 추가 분석 후 재점검"
    return {"verdict": verdict, "rule": rule, "max_weight_pp": max_w, "max_duration_diff": max_d,
            "max_hedge_pp": max_h, "missing": missing, "diffs": diffs,
            "thresholds": "일치: 비중≤2%p·듀레이션≤0.5y·헤지≤5%p / 부분: ≤5%p·≤1.0y·≤10%p"}


def generate_mp(asset: str, model: str | None = None) -> dict:
    client = anthropic.Anthropic()
    shared = build_shared_context()
    ev = limits.evaluate()
    context, extra = build_context(asset, ev)
    today = datetime.date.today()

    producer_agent = Agent(asset, shared, client, model=model)
    producer = producer_agent.opinion(load_prompt("mp_producer") + "\n\n# 입력 (CODE 계산)\n\n" + context,
                                      ModelPortfolio, mode="discuss")
    if producer is None:
        raise RuntimeError("산출자 패스 실패 (API 오류·거절·길이 초과)")
    verifier_agent = Agent(asset, shared, client, model=model)
    verifier = verifier_agent.opinion(load_prompt("mp_verifier") + "\n\n# 입력 (CODE 계산)\n\n" + context,
                                      ModelPortfolio, mode="discuss")

    p, v = producer.model_dump(), (verifier.model_dump() if verifier else None)
    doc = {
        "mp_id": store.new_id("MP", store.MP_DIR), "asset": asset, "asset_name": ASSET_KO[asset],
        "status": "DRAFT", "created_at": store.now_iso(),
        "valid_from": today.isoformat(), "valid_to": (today + datetime.timedelta(days=182)).isoformat(),
        "review_due": (today + datetime.timedelta(days=168)).isoformat(),
        "data_as_of": ev["as_of"], "analog_flags": extra["analog_flags"], "previous_mp_id": extra["current_mp_id"],
        "producer": p, "verifier": v, "comparison": compare_mp(p, v),
        "slots": {"claude_producer": "DONE", "claude_verifier": "DONE" if v else "FAILED", "chatgpt": "NOT_CONNECTED"},
        "targets": p["targets"],
        "meta": store.meta_block("producer+verifier", {"context": context}, model),
    }
    store.save_mp(doc)
    store.append_log("MP_GENERATED", {"mp_id": doc["mp_id"], "asset": asset, "verdict": doc["comparison"]["verdict"],
                                      "prompt_version": doc["meta"]["prompt_version"], "model_id": doc["meta"]["model_id"]})
    (config.REPORT_DIR / f"{doc['mp_id']}_{ASSET_KO[asset]}MP.md").write_text(to_markdown(doc), encoding="utf-8")
    return doc


def adopt_mp(mp_id: str, by: str = "운용역") -> dict:
    target = None
    for m in store.list_mps():
        if m["mp_id"] == mp_id:
            target = m
    if not target:
        raise KeyError(mp_id)
    for m in store.list_mps(target["asset"]):
        if m["status"] == "ADOPTED" and m["mp_id"] != mp_id:
            m["status"] = "EXPIRED"; m["expired_at"] = store.now_iso(); store.save_mp(m)
    target["status"] = "ADOPTED"; target["adopted_by"] = by; target["adopted_at"] = store.now_iso()
    store.save_mp(target)
    store.append_log("MP_ADOPTED", {"mp_id": mp_id, "asset": target["asset"], "by": by})
    return target


def to_markdown(doc: dict) -> str:
    p, v, c = doc["producer"], doc.get("verifier"), doc["comparison"]
    L = [f"# {doc['asset_name']} 모델 포트폴리오 — {doc['mp_id']} ({doc['status']})",
         f"기준일 {doc['data_as_of']} · 유효 {doc['valid_from']}~{doc['valid_to']} · 모델 {doc['meta']['model_id']} · 프롬프트 v{doc['meta']['prompt_version']}",
         "", f"**자체검증(동일 모델 2패스): {c['verdict']} — {c['rule']}**  (ChatGPT 슬롯: 미연결)", "",
         f"## 산출자 요약\n{p['summary']}  (확신도 {p['confidence']:.2f})", "",
         "| position_id | 버킷 | 목표 비중(%) | 밴드(±%p) | 듀레이션 | 헤지(%) | 비고 |", "|---|---|---|---|---|---|---|"]
    for t in p["targets"]:
        L.append(f"| {t['position_id']} | {t['bucket']} | {t.get('weight_pct') if t.get('weight_pct') is not None else '-'} | {t['band_pp']} | {t.get('duration') or '-'} | {t.get('hedge_pct') if t.get('hedge_pct') is not None else '-'} | {t.get('note','')} |")
    L += ["", "## 핵심 가정 [A]"] + [f"- {a['key']}: {a['value'] if a['value'] is not None else ''}{a['unit']} ({a['horizon']}) {a['text']}" for a in p["assumptions"]]
    L += ["", "## 근거"] + [f"- [{r['tag']}] {r['text']}" + (f" ({r['ref']})" if r.get("ref") else "") for r in p["rationale"]]
    L += ["", "## 재검토 조건"] + [f"- {t}" for t in p["review_triggers"]]
    if p.get("data_gaps"):
        L += ["", "## 데이터 공백"] + [f"- {g}" for g in p["data_gaps"]]
    if v:
        L += ["", f"## 검증자 요약\n{v['summary']}  (확신도 {v['confidence']:.2f})", "",
              "| position_id | 산출자 비중 | 검증자 비중 | 차이(%p) | 듀레이션 차 | 헤지 차(%p) |", "|---|---|---|---|---|---|"]
        for d in c.get("diffs", []):
            if "missing_in" in d:
                L.append(f"| {d['position_id']} | — | — | {d['missing_in']} 쪽 누락 | | |")
            else:
                L.append(f"| {d['position_id']} | {d.get('weight_producer','-')} | {d.get('weight_verifier','-')} | {d.get('weight_pp','-')} | {d.get('duration_diff','-')} | {d.get('hedge_pp','-')} |")
        L += ["", "검증자 재검토 조건: " + "; ".join(v["review_triggers"])]
    L += ["", "---", "본 문서는 AI 산출물(의사결정 지원 자료)로 운용역 채택·승인 전 효력이 없습니다."]
    return "\n".join(L)
