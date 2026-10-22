# -*- coding: utf-8 -*-
"""일 단위 상시 점검 — 코드가 신호를 만들고, 에이전트는 1분 브리프만 쓴다.

흐름: 전 포지션 손익·한도 소진율 산출 → 스트레스 → 손절선·한도 접근 경고 → 로그 → 보고서.
경고 레지스트리: 같은 (코드·버킷·등급) 신호는 하나의 경고로 유지하고, 조건이 해소되면 자동 CLOSED.
운용역 조치 입력(ACTIONED) → 독립 점검 확인(CONFIRMED) → CLOSED 전이는 act_alert() 로, 전부 로그에 남는다.
"""
import datetime
import os
import re

import anthropic

import config
import limits
import store
from agent import build_shared_context, load_prompt
from schemas import DailyBrief

GRADE_ORDER = limits.STAGE_ORDER
ASSET_KO = limits.ASSET_KO
OPEN_STATES = ("OPEN", "ACTIONED", "CONFIRMED")


def detect_shock(lim: dict) -> dict:
    """전일 대비 급변 감지 — 국고10y 일변동(bp), USD/KRW 일변동(%), VIX 레벨."""
    try:
        import history
        df = history.load_history().dropna(subset=["ktb_10y"], how="all")
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": str(e), "triggered": []}
    trig = lim.get("shock_triggers", {})
    out = {"available": True, "as_of": df.index[-1].date().isoformat(), "metrics": {}, "triggered": []}
    try:
        k = df["ktb_10y"].dropna()
        d_bp = round(float((k.iloc[-1] - k.iloc[-2]) * 100), 1)
        out["metrics"]["ktb_10y_daily_bp"] = d_bp
        if abs(d_bp) >= trig.get("ktb_10y_daily_bp", 20):
            out["triggered"].append(f"국고10y 일변동 {d_bp:+}bp (임계 ±{trig.get('ktb_10y_daily_bp')}bp)")
    except Exception:  # noqa: BLE001
        pass
    if "usdkrw" in df:
        u = df["usdkrw"].dropna()
        if len(u) >= 2:
            d_pct = round(float((u.iloc[-1] / u.iloc[-2] - 1) * 100), 2)
            out["metrics"]["usdkrw_daily_pct"] = d_pct
            if abs(d_pct) >= trig.get("usdkrw_daily_pct", 2.0):
                out["triggered"].append(f"USD/KRW 일변동 {d_pct:+}% (임계 ±{trig.get('usdkrw_daily_pct')}%)")
    if "vix" in df:
        vv = df["vix"].dropna()
        if len(vv):
            lvl = round(float(vv.iloc[-1]), 1)
            out["metrics"]["vix_level"] = lvl
            if lvl >= trig.get("vix_level", 30):
                out["triggered"].append(f"VIX {lvl} (임계 {trig.get('vix_level')})")
    return out


def _key(a: dict):
    return (a.get("code"), a.get("bucket"), a.get("grade"))


def _dedupe(code_alerts: list[dict]) -> list[dict]:
    """같은 (code, bucket, grade) 신호는 하나로 — 메시지는 합친다."""
    out, idx = [], {}
    for a in code_alerts:
        k = _key(a)
        if k in idx:
            out[idx[k]]["message"] += " / " + a["message"]
        else:
            idx[k] = len(out); out.append(dict(a))
    return out


def sync_alerts(code_alerts: list[dict]) -> dict:
    """경고 레지스트리 동기화 — ID 는 레지스트리(CLOSED 포함) 기준 당일 최대 순번+1 로 발급."""
    code_alerts = _dedupe(code_alerts)
    today = datetime.date.today().strftime("%Y%m%d")

    def _apply(reg: list[dict]):
        seq = 0
        for a in reg:
            m = re.fullmatch(rf"ALT-{today}-(\d+)", a.get("alert_id", "") or "")
            if m:
                seq = max(seq, int(m.group(1)))
        open_by_key = {_key(a): a for a in reg if a.get("status") in OPEN_STATES}
        new, closed, seen = [], [], set()
        for a in code_alerts:
            k = _key(a)
            seen.add(k)
            if k in open_by_key:
                cur = open_by_key[k]
                cur["metric"] = a["metric"]; cur["message"] = a["message"]; cur["last_seen"] = store.now_iso()
                cur["days_open"] = (datetime.date.today() - datetime.date.fromisoformat(cur["opened_at"][:10])).days
            else:
                seq += 1
                rec = {**a, "alert_id": f"ALT-{today}-{seq:02d}", "status": "OPEN", "opened_at": store.now_iso(),
                       "last_seen": store.now_iso(), "days_open": 0, "action": None, "confirm": None, "history": []}
                reg.append(rec); new.append(rec); open_by_key[k] = rec
                store.append_log("ALERT", {"alert_id": rec["alert_id"], "grade": rec["grade"], "bucket": rec["bucket"],
                                           "code": rec["code"], "metric": rec["metric"], "required_action": rec["required_action"], "due": rec["due"]})
        for k, a in open_by_key.items():
            if k not in seen and a["status"] != "CLOSED":
                a["status"] = "CLOSED"; a["closed_at"] = store.now_iso(); a["close_reason"] = "조건 해소(자동)"
                closed.append(a)
                store.append_log("CLOSE", {"alert_id": a["alert_id"], "reason": "조건 해소(자동)"})
        return {"open": [x for x in reg if x["status"] in OPEN_STATES], "new": new, "closed": closed}

    return store.update_alerts(_apply)


def act_alert(alert_id: str, status: str, by: str, note: str = "", amount_bn: float | None = None) -> dict:
    allowed = {"OPEN": ("ACTIONED",), "ACTIONED": ("CONFIRMED", "OPEN"), "CONFIRMED": ("CLOSED",)}

    def _apply(reg: list[dict]):
        cands = [a for a in reg if a.get("alert_id") == alert_id]
        if not cands:
            raise KeyError(alert_id)
        a = next((x for x in cands if x.get("status") in OPEN_STATES), cands[-1])  # 열린 레코드 우선
        if status not in allowed.get(a["status"], ()):
            raise ValueError(f"{a['status']} → {status} 전이는 허용되지 않음")
        entry = {"status": status, "by": by, "note": note, "amount_bn": amount_bn, "at": store.now_iso()}
        a["status"] = status
        a.setdefault("history", []).append(entry)
        if status == "ACTIONED":
            a["action"] = entry
        elif status == "CONFIRMED":
            a["confirm"] = entry
        elif status == "CLOSED":
            a["closed_at"] = entry["at"]; a["close_reason"] = note or "운용역·독립점검 종결"
        store.append_log({"ACTIONED": "ACTION", "CONFIRMED": "CONFIRM", "CLOSED": "CLOSE", "OPEN": "REOPEN"}[status],
                         {"alert_id": alert_id, "by": by, "note": note, "amount_bn": amount_bn})
        return a

    return store.update_alerts(_apply)


def _brief_template(ev: dict, alerts_open: list[dict], shock: dict, gaps: list[dict]) -> dict:
    n_w = sum(1 for a in alerts_open if a["grade"] == "WARN")
    n_r = sum(1 for a in alerts_open if a["grade"] == "REDUCE")
    n_h = sum(1 for a in alerts_open if a["grade"] == "HALT")
    head = "중단 신호 발생" if n_h else (f"축소 {n_r}건" + (f" · 경고 {n_w}건" if n_w else "")) if n_r else (f"경고 {n_w}건" if n_w else "정상 — 발동 신호 없음")
    interp = [f"[F] 전체 소진율 {ev['total']['util_pct']}% ({limits.STAGE_KO[ev['total']['stage']]}), "
              + ", ".join(f"{ASSET_KO[a]} {u['util_pct']}%" for a, u in ev["by_asset"].items())]
    if ev.get("worst_stress"):
        w = next(s for s in ev["stress"] if s["name"] == ev["worst_stress"])
        interp.append(f"[F] 최악 스트레스 '{w['name']}' 손실 {w['loss_total_bn']}억 → 전체 소진율 {w['post_util_total_pct']}%")
    if gaps:
        g = gaps[0]
        interp.append(f"[F] MP 괴리 최대: {g['bucket']} {g['gap_pp']}%p (밴드 ±{g['band_pp']})")
    if shock.get("triggered"):
        interp.append("[F] 시장 급변: " + "; ".join(shock["triggered"]))
    todo = [f"{a['grade_ko']} {a['bucket']}: {a['required_action']} (기한 {a['due']})" for a in alerts_open if a["status"] == "OPEN"]
    return {"headline": head, "interpretation": interp[:3], "todo": todo or ["특이 조치 없음"], "source": "CODE(템플릿)"}


def run_daily(model: str | None = None, write_brief: bool = True) -> dict:
    # 보유 엑셀(시험 포트)이 있으면 먼저 시가 평가해 positions.json 을 갱신한다
    book_res = None
    try:
        import book
        if book.DEFAULT_BOOK.exists():
            book_res = book.sync(quiet=True)
    except Exception as e:  # noqa: BLE001 — 평가 실패 시 기존 positions.json 으로 진행
        book_res = {"error": f"{type(e).__name__}: {e}"}
    try:
        import quant
        quant.run()      # 기술적 매력도·예상손익 갱신 (실패해도 점검은 계속)
    except Exception as e:  # noqa: BLE001
        print(f"  정량 분석 실패: {e}")
    try:
        import equity
        equity.build()   # 주식 대시보드(공개 가격) — 실패해도 점검은 계속
    except Exception as e:  # noqa: BLE001
        print(f"  주식 대시보드 실패: {e}")
    try:
        import strategy
        strategy.build()  # 전략 보드(지금 할 일) — 에이전트 컨텍스트에도 들어간다
    except Exception as e:  # noqa: BLE001
        print(f"  전략 보드 실패: {e}")
    lim = limits.load_limits()
    ev = limits.evaluate(limits=lim)
    shock = detect_shock(lim)
    gaps = []
    for asset in ("bond", "equity", "fx"):
        mp = store.load_mp(asset, adopted_only=True)
        if mp:
            gaps += limits.gap_analysis(ev, {asset: mp}, band_pp=lim.get("mp_band_pp", 2.0))
    gaps.sort(key=lambda r: -abs(r["gap_pp"] or 0))

    code_alerts = list(ev["alerts"])
    if shock.get("triggered"):
        code_alerts.append({"code": "SHOCK", "grade": "WARN", "grade_ko": "경고", "bucket": "시장 급변",
                            "metric": {"name": "shock", "value": "; ".join(shock["triggered"]), "threshold": None, "as_of": shock.get("as_of"), "source": "CODE"},
                            "message": "시장 급변 트리거: " + "; ".join(shock["triggered"]),
                            "required_action": "투자 포인트(시장 급변) 점검 실행", "due": "당일"})
    synced = sync_alerts(code_alerts)
    open_alerts = synced["open"]

    pending_checks = [c for c in store.list_checks() if (c.get("decision") or {}).get("status") == "PENDING"]
    suggested = []
    if any(a["grade"] in ("WARN", "REDUCE", "HALT") and a["code"] in ("UTIL_STAGE", "STOP_HIT", "STOP_APPROACH") for a in open_alerts):
        suggested.append("LIMIT_APPROACH")
    if shock.get("triggered"):
        suggested.append("SHOCK")

    brief = _brief_template(ev, open_alerts, shock, gaps)
    brief_source = "CODE(템플릿)"
    if write_brief and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            ctx = "\n\n".join([limits.to_markdown(ev),
                               "### 열린 경고\n" + ("\n".join(f"- [{a['grade_ko']}] {a['message']} (상태 {a['status']}, {a['days_open']}일째)" for a in open_alerts) or "- 없음"),
                               "### 시장 급변 감지\n" + ("; ".join(shock.get("triggered") or ["없음"]) + f" / 지표 {shock.get('metrics')}"),
                               "### MP 괴리 상위\n" + ("\n".join(f"- {g['bucket']}: {g['gap_pp']}%p (밴드 ±{g['band_pp']}, {g['status']})" for g in gaps[:5]) or "- 채택 MP 없음"),
                               "### 보류 중 점검 보고서\n" + ("\n".join(f"- {c['report_id']} {c['trigger']} {c['verdict']}" for c in pending_checks) or "- 없음")])
            client = anthropic.Anthropic()
            resp = client.messages.parse(
                model=model or config.MODEL, max_tokens=4000, output_config={"effort": "medium"},
                system=[{"type": "text", "text": build_shared_context(), "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": load_prompt("daily_brief")}],
                messages=[{"role": "user", "content": ctx}], output_format=DailyBrief)
            if resp.stop_reason not in ("refusal", "max_tokens") and resp.parsed_output:
                brief = resp.parsed_output.model_dump(); brief_source = "AGENT"
        except Exception as e:  # noqa: BLE001 — 브리프 실패는 템플릿으로 대체
            brief["note"] = f"에이전트 브리프 실패, 템플릿 사용: {e}"
    brief["source"] = brief_source

    # 에이전트 룸 — 각 담당자의 '오늘의 보고' (API 키 없으면 코드 템플릿 대사)
    try:
        import voice
        shared_data = voice.gather(ev)
        shared_data["shock"] = shock
        shared_data["alerts_open"] = open_alerts
        shared_data["pending"] = pending_checks
        voices = voice.speak_all(ev=ev, model=model, use_api=write_brief, d=shared_data)
    except Exception as e:  # noqa: BLE001
        voices = {"error": str(e)}

    day = datetime.date.today().isoformat()
    doc = {"day": day, "ran_at": store.now_iso(), "data_as_of": ev["as_of"], "pnl_as_of_min": ev["pnl_as_of_min"],
           "total": ev["total"], "by_asset": ev["by_asset"], "risk_floor": ev["risk_floor"], "limit_breach": ev["limit_breach"],
           "worst_stress": ev["worst_stress"], "stress": [{k: s[k] for k in ("id", "name", "loss_total_bn", "post_util_total_pct", "post_stage_total")} for s in ev["stress"]],
           "validation": ev["validation"], "shock": shock, "gaps": gaps[:10],
           "alerts_open": open_alerts, "alerts_new": [a["alert_id"] for a in synced["new"]], "alerts_closed": [a["alert_id"] for a in synced["closed"]],
           "suggested_triggers": suggested, "pending_checks": [c["report_id"] for c in pending_checks],
           "brief": brief, "voices": voices, "is_sample": ev["is_sample"], "book": book_res,
           "meta": store.meta_block("daily", {"as_of": ev["as_of"], "alerts": len(open_alerts)}, model)}
    store.save_daily(day, doc)
    store.append_log("DAILY_RUN", {"day": day, "total_util_pct": ev["total"]["util_pct"], "stage": ev["total"]["stage"],
                                   "alerts_open": len(open_alerts), "new": len(synced["new"]), "closed": len(synced["closed"])})
    (config.REPORT_DIR / f"{day}_일일점검.md").write_text(to_markdown(doc, ev), encoding="utf-8")
    return doc


def to_markdown(doc: dict, ev: dict | None = None) -> str:
    b = doc["brief"]
    L = [f"# 일일 상시 점검 — {doc['day']}", f"실행 {doc['ran_at']} · 포지션 기준일 {doc['data_as_of']} (최저 {doc['pnl_as_of_min']}) · 브리프 출처 {b.get('source')}", "",
         f"## {b['headline']}"] + [f"- {s}" for s in b["interpretation"]] + ["", "**오늘 할 일**"] + [f"- {t}" for t in b["todo"]]
    L += ["", "## 한도 소진율 (CODE)", "| 범위 | 소진율(%) | 단계 | 손실(억)/한도(억) |", "|---|---|---|---|",
          f"| 전체 | {doc['total']['util_pct']} | {limits.STAGE_KO[doc['total']['stage']]} | {doc['total']['used_bn']}/{doc['total']['limit_bn']} |"]
    for a, u in doc["by_asset"].items():
        L.append(f"| {ASSET_KO[a]} | {u['util_pct']} | {limits.STAGE_KO[u['stage']]} | {u['used_bn']}/{u['limit_bn']} |")
    L += ["", "## 열린 경고"] + ([f"- [{a['grade_ko']}] {a['message']} — {a['required_action']} (기한 {a['due']}, 상태 {a['status']})" for a in doc["alerts_open"]] or ["- 없음"])
    L += ["", "## 스트레스 (CODE)"] + [f"- {s['id'] or ''} {s['name']}: 손실 {s['loss_total_bn']}억 → 전체 소진율 {s['post_util_total_pct']}% ({limits.STAGE_KO[s['post_stage_total']]})" for s in doc["stress"]]
    if doc["shock"].get("available"):
        L += ["", f"## 시장 급변 감지 (기준일 {doc['shock'].get('as_of')})", f"- 지표: {doc['shock'].get('metrics')}"] + ([f"- 트리거: {t}" for t in doc["shock"]["triggered"]] or ["- 트리거 없음"])
    if doc["gaps"]:
        L += ["", "## MP 괴리 상위 (CODE)"] + [f"- {g['bucket']}: 현 {g['now_pct']}% vs MP {g['mp_pct']}% → {g['gap_pp']}%p ({g['status']})" for g in doc["gaps"][:5]]
    if doc["validation"]["warnings"] or doc["validation"]["errors"]:
        L += ["", "## 데이터·한도 검증"] + [f"- {w}" for w in doc["validation"]["errors"] + doc["validation"]["warnings"]]
    if doc["suggested_triggers"]:
        L += ["", "## 권고: 투자 포인트 점검 실행 — " + ", ".join(doc["suggested_triggers"])]
    L += ["", "---", "에이전트는 경고·제안까지. 축소·청산 실행은 운용역, 확인은 독립 점검 기능."]
    return "\n".join(L)


# ── 투자위원회 월간 보고서 ──────────────────────────────────────
def monthly_report(month: str, model: str | None = None) -> str:
    """month = 'YYYY-MM'. 일일 스냅샷·로그·점검 보고서에서 코드 집계, 서술 요약 1단락만 에이전트(선택)."""
    days = store.list_daily(month)
    logs = store.read_log(limit=None, since=f"{month}-01")
    logs = [r for r in logs if r.get("ts", "").startswith(month)]
    checks = [c for c in store.list_checks() if (c.get("created_at") or "").startswith(month)]
    alerts_log = [r for r in logs if r["kind"] == "ALERT"]
    actions = {r["alert_id"]: r for r in logs if r["kind"] == "ACTION"}
    resp_hours = []
    for a in alerts_log:
        act = actions.get(a["alert_id"])
        if act:
            try:
                t0 = datetime.datetime.fromisoformat(a["ts"]); t1 = datetime.datetime.fromisoformat(act["ts"])
                resp_hours.append((t1 - t0).total_seconds() / 3600)
            except ValueError:
                pass
    by_grade = {g: sum(1 for a in alerts_log if a["grade"] == g) for g in ("WARN", "REDUCE", "HALT")}
    L = [f"# 투자위원회 보고 — AI 운용관리 체계 월간 ({month})", f"생성 {store.now_iso()} · 자료: 일일 점검 {len(days)}회, 로그 {len(logs)}건, 점검 보고서 {len(checks)}건", "",
         "## 1. 기간 요약 (CODE 집계)",
         f"- 경고 발생: 경고 {by_grade['WARN']} / 축소 {by_grade['REDUCE']} / 중단 {by_grade['HALT']}건",
         f"- 조치 입력 {len(actions)}건, 평균 대응 {round(sum(resp_hours)/len(resp_hours),1) if resp_hours else '-'}시간",
         f"- 로그 무결성: {store.verify_log_chain()}", ""]
    if days:
        L += ["## 2. 한도 소진율 추이", "| 일자 | 전체 | 채권 | 주식 | 환 | 단계 |", "|---|---|---|---|---|---|"]
        for d in days:
            L.append(f"| {d['day']} | {d['total']['util_pct']} | {d['by_asset']['bond']['util_pct']} | {d['by_asset']['equity']['util_pct']} | {d['by_asset']['fx']['util_pct']} | {limits.STAGE_KO[d['total']['stage']]} |")
        mx = max(days, key=lambda d: d["total"]["util_pct"])
        L += [f"\n월중 최고 {mx['total']['util_pct']}% ({mx['day']}), 월말 {days[-1]['total']['util_pct']}%", ""]
    L += ["## 3. 투자 포인트 점검 보고서", "| ID | 트리거 | 자산 | 등급 | 자체검증 | 규칙 | 결정 |", "|---|---|---|---|---|---|---|"]
    for c in checks:
        d = c.get("decision") or {}
        L.append(f"| {c['report_id']} | {c['trigger']} | {c['asset']} | {c['final_grade']} | {c['verdict']} | {c['rule']} | {d.get('status')} {d.get('by') or ''} |")
    adopted = sum(1 for c in checks if (c.get("decision") or {}).get("status") in ("ADOPTED", "MODIFIED"))
    L += [f"\n조정안 채택률: {adopted}/{len(checks)}" if checks else "\n(해당 월 점검 보고서 없음)", "",
          "## 4. 미결·재승인 안건"]
    open_alerts = [a for a in store.load_alerts() if a["status"] in OPEN_STATES]
    L += [f"- [{a['grade_ko']}] {a['bucket']} — {a['status']}, {a.get('days_open', 0)}일째 (기한 {a['due']})" for a in open_alerts] or ["- 없음"]
    versions = sorted({(r.get("model_id"), r.get("prompt_version")) for r in logs if r.get("prompt_version")})
    L += ["", "## 5. 모델·프롬프트 버전 이력"] + ([f"- {m} / 프롬프트 v{p}" for m, p in versions] or ["- 기록 없음"])
    L += ["", "---", "본 보고서의 수치는 코드 집계(CODE)이며, AI 산출물은 운용역 결정·승인 전 효력이 없습니다."]
    text = "\n".join(L)
    (config.REPORT_DIR / f"투자위보고_{month}.md").write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    import json
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = run_daily()
    print(json.dumps(d["brief"], ensure_ascii=False, indent=1))
    print("open alerts:", len(d["alerts_open"]), "suggested:", d["suggested_triggers"])
