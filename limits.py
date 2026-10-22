# -*- coding: utf-8 -*-
"""손실한도·손절·스트레스 엔진 — 전부 결정적 계산 (LLM 개입 없음).

3계층: 전체 한도·자산별 한도는 **소진율**로, 포지션은 **손절선**으로 각각 판정한다.
하위 계층 신호는 상위 계층의 '단계'로 승격되지 않고 별도 필드(position_stage_max)와 리스크 등급 하한(floor)에만 반영된다.
단계: NORMAL / WATCH / WARN(경고) / REDUCE(축소) / HALT(중단).

손실 정의: YTD 평가손익 기준, 환헤지 비용 차감 후. 레벨 내 순손익 상계. 소진율 = max(0, -순손익)/한도.
- 환 한도: 환오픈분(익스포저 × (1-헤지비율))의 환 손익(pnl_fx_ytd_bn)에만 적용.
  asset=fx 포지션(환 오버레이: 달러·엔 선물 등)은 현금 다리 없이 명목(fx_exposure_bn)이 그대로 환오픈이 된다.
- rate_pnl_in_scope=false(ALM 보유분): 금리 평가손익을 한도 대상에서 제외 — `pnl_rate_ytd_bn` 이 있으면 손익에서 빼고,
  없으면 전액 포함하되 데이터 경고를 남긴다. 스트레스에서도 금리 충격을 제외한다(스프레드 충격은 포함).
- 위탁 혼합 포트(mixed)는 look_through 비율로 주식/채권 다리(leg)로 나눠 집계. 손익 분리 보고(pnl_split)가 없으면 비중 추정(경고).
- 스트레스 후 소진율 = max(0, 시나리오 손실 − 현재 순손익)/한도 (현재 이익이 손실을 상계).
- 손절선: 손절 접근률 = 손실/손절금액(|손절%| × 기준액). 70% 접근 → 경고, 100% 도달 → 축소. 자산 손절과 환오픈 손절은 따로 판정.
"""
import datetime

import config
from agent import load_json

STAGE_ORDER = {"NORMAL": 0, "WATCH": 1, "WARN": 2, "REDUCE": 3, "HALT": 4}
STAGE_KO = {"NORMAL": "정상", "WATCH": "관찰", "WARN": "경고", "REDUCE": "축소", "HALT": "중단"}
ASSET_KO = {"bond": "채권", "equity": "주식", "fx": "환", "mixed": "혼합(위탁)"}
ASSETS = ("bond", "equity", "fx")
REQUIRED_LIMIT_KEYS = ("total_limit_bn", "asset_limits_bn", "stages", "stop_loss", "stress_scenarios", "deadlines")


def _f(x, default=0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def load_positions() -> dict:
    """최소 스키마 검증 — 깨진 항목은 건너뛰고 meta.load_warnings 에 사유를 남긴다."""
    doc = load_json(config.DATA_DIR / "positions.json") or load_json(
        config.DATA_DIR / "positions.sample.json") or {"meta": {}, "positions": []}
    good, warns = [], []
    for i, p in enumerate(doc.get("positions", []) or []):
        # asset=fx 는 환 오버레이(통화선물 등): 현금 없음(mv_bn 0), fx_exposure_bn 명목이 곧 환오픈 → 환 다리만 생긴다
        if not isinstance(p, dict) or not p.get("id") or not p.get("name") or p.get("asset") not in ("bond", "equity", "mixed", "fx"):
            warns.append(f"positions[{i}]: id/name/asset(bond|equity|mixed|fx) 누락 — 제외")
            continue
        try:
            float(p.get("mv_bn")); float(p.get("pnl_ytd_bn", 0))
        except (TypeError, ValueError):
            warns.append(f"{p.get('id')}: mv_bn/pnl_ytd_bn 숫자 아님 — 제외")
            continue
        good.append(p)
    doc = dict(doc); doc["positions"] = good
    doc.setdefault("meta", {})["load_warnings"] = warns
    return doc


def load_limits() -> dict:
    lim = load_json(config.DATA_DIR / "limits.json")
    if lim and all(k in lim for k in REQUIRED_LIMIT_KEYS):
        return lim
    sample = load_json(config.DATA_DIR / "limits.sample.json")
    if lim:
        sample = dict(sample); sample.setdefault("meta", {})["load_warning"] = \
            f"limits.json 에 필수 키 누락({[k for k in REQUIRED_LIMIT_KEYS if k not in lim]}) — 가안(limits.sample.json) 사용"
    return sample


def is_sample(name: str) -> bool:
    return not (config.DATA_DIR / f"{name}.json").exists()


def _eff_pnl(p: dict, warns: list[str]) -> float:
    """한도 대상 손익: in_scope=false 면 금리 손익(pnl_rate_ytd_bn)을 제외."""
    pnl = _f(p.get("pnl_ytd_bn"))
    if p.get("rate_pnl_in_scope", True):
        return pnl
    if p.get("pnl_rate_ytd_bn") is None:
        warns.append(f"{p['name']}: rate_pnl_in_scope=false 이나 pnl_rate_ytd_bn 없음 — 금리 손익 전액 포함해 계산")
        return pnl
    return pnl - _f(p.get("pnl_rate_ytd_bn"))


# ── 다리(leg) 분해 ──────────────────────────────────────────────
def expand_legs(positions: list[dict], warns: list[str] | None = None) -> list[dict]:
    warns = warns if warns is not None else []
    legs = []
    for p in positions:
        asset = p.get("asset")
        mv = _f(p.get("mv_bn"))
        pnl = _eff_pnl(p, warns)
        in_scope = bool(p.get("rate_pnl_in_scope", True))
        if asset == "mixed":
            lt = p.get("look_through", {})
            eq_w = _f(lt.get("equity_pct")) / 100
            bd_w = _f(lt.get("bond_pct"), 100 - _f(lt.get("equity_pct"))) / 100
            split = p.get("pnl_split") or {}
            est = split.get("equity") is None or split.get("bond") is None
            if est:
                warns.append(f"{p['name']}: 손익 {pnl:.0f}억을 look-through 비중 {eq_w*100:.0f}/{bd_w*100:.0f} 으로 추정 분할 — 운용사 보고서의 자산별 손익(pnl_split)으로 교체 필요")
            legs.append({"pos_id": p["id"], "name": p["name"] + " [주식분]", "asset": "equity",
                         "mv_bn": mv * eq_w, "pnl_bn": _f(split.get("equity"), pnl * eq_w),
                         "beta": _f(lt.get("equity_beta"), 1.0), "estimated": est})
            legs.append({"pos_id": p["id"], "name": p["name"] + " [채권분]", "asset": "bond",
                         "mv_bn": mv * bd_w, "pnl_bn": _f(split.get("bond"), pnl * bd_w),
                         "duration": _f(lt.get("bond_duration"), 4.0),
                         "spread_duration": _f(lt.get("bond_spread_duration"), 2.0),
                         "spread_beta": _f(lt.get("bond_spread_beta"), 1.0),
                         "rate_in_scope": in_scope, "estimated": est})
        elif asset in ("bond", "equity"):
            legs.append({"pos_id": p["id"], "name": p["name"], "asset": asset, "mv_bn": mv, "pnl_bn": pnl,
                         "duration": _f(p.get("duration")), "spread_duration": _f(p.get("spread_duration")),
                         "spread_beta": _f(p.get("spread_beta"), 1.0), "beta": _f(p.get("beta"), 1.0),
                         "rate_in_scope": in_scope, "estimated": False})
        if p.get("currency") and p.get("currency") != "KRW":
            exposure = _f(p.get("fx_exposure_bn"), mv)
            hedge = _f(p.get("hedge_pct"), 100)
            legs.append({"pos_id": p["id"], "name": p["name"] + " [환오픈분]", "asset": "fx",
                         "mv_bn": exposure * (1 - hedge / 100), "exposure_bn": exposure, "hedge_pct": hedge,
                         "pnl_bn": _f(p.get("pnl_fx_ytd_bn")), "estimated": False})
    return legs


# ── 단계 판정 ────────────────────────────────────────────────────
def stage_for(util_pct: float, stages: dict) -> str:
    if util_pct >= stages["halt_pct"]:
        return "HALT"
    if util_pct >= stages["reduce_pct"]:
        return "REDUCE"
    if util_pct >= stages["warn_pct"]:
        return "WARN"
    return "NORMAL"


def _max_stage(*stages: str) -> str:
    return max(stages, key=lambda s: STAGE_ORDER[s]) if stages else "NORMAL"


def _usage(pnl_sum: float, limit_bn: float) -> dict:
    used = max(0.0, -pnl_sum)
    return {"limit_bn": limit_bn, "pnl_bn": round(pnl_sum, 1), "used_bn": round(used, 1),
            "headroom_bn": round(limit_bn - used, 1),
            "util_pct": round(used / limit_bn * 100, 1) if limit_bn else 0.0}


def _stop_stage(ratio_pct: float, approach_ratio: float) -> str:
    if ratio_pct >= 100:
        return "REDUCE"
    if ratio_pct >= approach_ratio * 100:
        return "WARN"
    return "NORMAL"


# ── 스트레스 ─────────────────────────────────────────────────────
def leg_stress_loss(leg: dict, sc: dict) -> float:
    """시나리오 손실(억원, 양수 = 손실). 부호: rate_bp + 금리상승, spread_bp + 확대, equity_pct + 상승, usdkrw_pct + 원화약세."""
    mv = leg["mv_bn"]
    if leg["asset"] == "bond":
        rate = leg.get("duration", 0) * sc.get("rate_bp", 0) / 10000 * mv if leg.get("rate_in_scope", True) else 0.0
        spread = leg.get("spread_duration", 0) * leg.get("spread_beta", 1.0) * sc.get("spread_bp", 0) / 10000 * mv
        return rate + spread
    if leg["asset"] == "equity":
        return -leg.get("beta", 1.0) * sc.get("equity_pct", 0) / 100 * mv
    if leg["asset"] == "fx":
        return -sc.get("usdkrw_pct", 0) / 100 * mv  # 원화 강세(음수) → 미헤지 USD 손실
    return 0.0


def _post_util(loss: float, pnl: float, limit: float) -> float | None:
    """스트레스 후 소진율 — 현재 순손익으로 상계."""
    if not limit:
        return None
    return round(max(0.0, loss - pnl) / limit * 100, 1)


# ── 메인 평가 ────────────────────────────────────────────────────
def evaluate(positions_doc: dict | None = None, limits: dict | None = None,
             new_position: dict | None = None) -> dict:
    positions_doc = positions_doc or load_positions()
    limits = limits or load_limits()
    positions = [dict(p) for p in positions_doc.get("positions", [])]
    stages = limits["stages"]
    sl_cfg = limits["stop_loss"]
    approach = _f(sl_cfg.get("approach_ratio"), 0.7)
    tol = limits.get("staleness_tolerance_days", {"bucket": 3, "mandate": 10})
    today = datetime.date.today()
    as_of = positions_doc.get("meta", {}).get("as_of")
    warnings = list(positions_doc.get("meta", {}).get("load_warnings") or [])
    if limits.get("meta", {}).get("load_warning"):
        warnings.append(limits["meta"]["load_warning"])

    if new_position:
        np_ = {"name": "신규 포지션", "type": "bucket", "pnl_ytd_bn": 0.0, "pnl_fx_ytd_bn": 0.0, **new_position}
        np_["id"] = "NEW"
        np_["name"] = f"[신규] {np_['name']}"
        np_["short_name"] = np_["name"][:24]
        np_.setdefault("base_bn", np_.get("mv_bn"))
        positions.append(np_)

    legs = expand_legs(positions, warnings)
    total_mv = sum(_f(p.get("mv_bn")) for p in positions)
    asset_limits = {a: _f(limits["asset_limits_bn"].get(a)) for a in ASSETS}
    total_limit = _f(limits["total_limit_bn"])

    # ── 포지션별 손절선 (자산 손절·환오픈 손절 분리) ──
    pos_rows, stop_budget = [], {a: 0.0 for a in ASSETS}
    for p in positions:
        mv = _f(p.get("mv_bn"))
        pnl = _eff_pnl(p, [])  # 경고는 expand_legs 에서 이미 기록
        base = _f(p.get("base_bn"), mv - pnl) or mv
        sl = _f(p.get("stop_loss_pct"), sl_cfg["default_pct"].get(p.get("asset"), -5.0))
        stop_amt = abs(sl) / 100 * base
        ratio = (max(0.0, -pnl) / stop_amt * 100) if stop_amt else 0.0
        stage_asset = _stop_stage(ratio, approach)
        if p.get("asset") == "mixed":
            # 위탁 손절은 전체 손익에 한 번 걸리므로 어느 다리에서든 전액 발생 가능 → 양쪽 예산에 보수적으로 전액 계상
            stop_budget["equity"] += stop_amt; stop_budget["bond"] += stop_amt
        elif p.get("asset") in ("bond", "equity"):
            stop_budget[p["asset"]] += stop_amt
        fx = None
        if p.get("currency") and p.get("currency") != "KRW":
            exposure = _f(p.get("fx_exposure_bn"), mv)
            open_bn = exposure * (1 - _f(p.get("hedge_pct"), 100) / 100)
            fx_sl = _f(p.get("fx_stop_pct"), sl_cfg.get("fx_open_default_pct", -6.0))
            fx_stop_amt = abs(fx_sl) / 100 * open_bn
            stop_budget["fx"] += fx_stop_amt
            fx_ratio = (max(0.0, -_f(p.get("pnl_fx_ytd_bn"))) / fx_stop_amt * 100) if fx_stop_amt else 0.0
            fx = {"open_bn": round(open_bn, 1), "pnl_fx_bn": _f(p.get("pnl_fx_ytd_bn")), "stop_pct": fx_sl,
                  "stop_amount_bn": round(fx_stop_amt, 1), "stop_ratio_pct": round(fx_ratio, 1),
                  "stage": _stop_stage(fx_ratio, approach)}
        reasons = []
        if stage_asset != "NORMAL":
            reasons.append("손절선 " + ("도달" if stage_asset == "REDUCE" else "접근"))
        if fx and fx["stage"] != "NORMAL":
            reasons.append("환오픈 손절선 " + ("도달" if fx["stage"] == "REDUCE" else "접근"))
        stale, age = False, None
        if p.get("pnl_as_of"):
            try:
                age = (today - datetime.date.fromisoformat(p["pnl_as_of"])).days
                stale = age > tol.get(p.get("type", "bucket"), 3)
            except ValueError:
                pass
        if stale:
            warnings.append(f"{p['name']} 손익 기준일 {p['pnl_as_of']}({age}일 경과) — 포함했으나 확정치 아님")
        pos_rows.append({
            "id": p["id"], "name": p.get("short_name") or p["name"], "full_name": p["name"],
            "asset": p.get("asset"), "type": p.get("type", "bucket"),
            "mv_bn": round(mv, 1), "weight_pct": round(mv / total_mv * 100, 2) if total_mv else 0,
            "pnl_ytd_bn": round(pnl, 1), "pnl_pct": round(pnl / base * 100, 2) if base else 0,
            "stop_loss_pct": sl, "stop_amount_bn": round(stop_amt, 1), "stop_ratio_pct": round(ratio, 1),
            "approach_ratio_pct": round(approach * 100), "stage_asset": stage_asset,
            "stage": _max_stage(stage_asset, fx["stage"] if fx else "NORMAL"), "reason": " · ".join(reasons),
            "fx": fx, "pnl_as_of": p.get("pnl_as_of"), "stale": stale,
            "duration": p.get("duration"), "beta": p.get("beta"),
            "hedge_pct": p.get("hedge_pct"), "currency": p.get("currency", "KRW"),
            "rate_pnl_in_scope": bool(p.get("rate_pnl_in_scope", True)), "is_new": p["id"] == "NEW",
            "pnl_split_estimated": p.get("asset") == "mixed" and not (p.get("pnl_split") or {}).get("equity"),
        })

    def pos_stage_max(asset: str) -> str:
        if asset == "fx":
            return _max_stage(*[r["fx"]["stage"] for r in pos_rows if r["fx"] and not r["is_new"]])
        return _max_stage(*[r["stage_asset"] for r in pos_rows if r["asset"] in (asset, "mixed") and not r["is_new"]])

    # ── 자산별·전체 소진율 (단계는 소진율로만; 중단 override 적용) ──
    halted = set(limits.get("overrides", {}).get("halted", []))
    by_asset = {}
    for a in ASSETS:
        u = _usage(sum(l["pnl_bn"] for l in legs if l["asset"] == a), asset_limits[a])
        u["stage"] = "HALT" if a in halted else stage_for(u["util_pct"], stages)
        u["position_stage_max"] = pos_stage_max(a)
        u["mv_bn"] = round(sum(l["mv_bn"] for l in legs if l["asset"] == a), 1)
        u["stop_budget_used_bn"] = round(stop_budget[a], 1)
        u["stop_budget_free_bn"] = round(asset_limits[a] - stop_budget[a], 1)
        by_asset[a] = u
    total = _usage(sum(l["pnl_bn"] for l in legs), total_limit)
    total["stage"] = "HALT" if "total" in halted else stage_for(total["util_pct"], stages)
    total["position_stage_max"] = _max_stage(*[u["position_stage_max"] for u in by_asset.values()])

    # ── 계층 검증 ──
    errors = []
    if sum(asset_limits.values()) > total_limit + 1e-9:
        errors.append(f"자산별 한도 합계 {sum(asset_limits.values()):.0f}억 > 전체 한도 {total_limit:.0f}억")
    for a in ASSETS:
        if stop_budget[a] > asset_limits[a] + 1e-9:
            warnings.append(f"{ASSET_KO[a]} 손절금액 합계 {stop_budget[a]:.0f}억 > {ASSET_KO[a]} 한도 {asset_limits[a]:.0f}억 (위탁 손절은 양쪽 자산에 전액 계상) — 손절선 또는 한도 재설정 필요")

    # ── 스트레스 (현재 순손익 상계) ──
    stress, sizing = [], None
    pnl_a = {a: by_asset[a]["pnl_bn"] for a in ASSETS}
    for sc in limits["stress_scenarios"]:
        loss = {a: 0.0 for a in ASSETS}
        for l in legs:
            loss[l["asset"]] += leg_stress_loss(l, sc)
        loss_total = sum(loss.values())
        post = {a: _post_util(loss[a], pnl_a[a], asset_limits[a]) for a in ASSETS}
        post_total = _post_util(loss_total, total["pnl_bn"], total_limit) or 0.0
        row = {"id": sc.get("id"), "name": sc["name"], "desc": sc.get("desc", ""),
               "sizing": bool(sc.get("sizing")), "reference": bool(sc.get("reference")),
               "loss_bn": {a: round(v, 1) for a, v in loss.items()}, "loss_total_bn": round(loss_total, 1),
               "post_util_pct": post, "post_util_total_pct": post_total,
               "post_stage_total": stage_for(post_total, stages),
               "post_stage_by_asset": {a: stage_for(post[a], stages) if post[a] is not None else "NORMAL" for a in ASSETS}}
        stress.append(row)
        if row["sizing"]:
            sizing = (sc, row)
    scoring = [s for s in stress if not s["sizing"] and not s["reference"]] or [s for s in stress if not s["sizing"]] or stress
    worst = max(scoring, key=lambda s: s["loss_total_bn"]) if scoring else None
    stress_refs = [f"[참고] {s['name']} 적용 시 전체 소진율 {s['post_util_total_pct']}% ({STAGE_KO[s['post_stage_total']]})"
                   for s in scoring if s["post_stage_total"] in ("REDUCE", "HALT")]

    # ── 축소 규모·추가 가능 규모 (S0 기준, 목표 = 축소 임계) ──
    target_pct = stages["reduce_pct"] / 100
    suggested_cut, max_addable = {}, None
    if sizing:
        sc0, row0 = sizing
        for a in ASSETS:
            if by_asset[a]["stage"] in ("REDUCE", "HALT") and by_asset[a]["mv_bn"] > 0:
                excess = max(0.0, (row0["loss_bn"][a] - pnl_a[a]) - asset_limits[a] * target_pct)
                rate = row0["loss_bn"][a] / by_asset[a]["mv_bn"] if row0["loss_bn"][a] > 0 else 0
                suggested_cut[a] = round(excess / rate, 0) if rate > 0 else None
        if new_position:
            new_mv = max(_f(new_position.get("mv_bn")), 1e-9)
            new_legs = [l for l in legs if l["pos_id"] == "NEW"]
            per_bn = {a: sum(leg_stress_loss(l, sc0) for l in new_legs if l["asset"] == a) / new_mv for a in ASSETS}
            per_bn_total = sum(per_bn.values())
            caps, blockers = {}, []
            for a in ASSETS:
                base_loss = row0["loss_bn"][a] - per_bn[a] * new_mv
                base_post = _post_util(base_loss, pnl_a[a], asset_limits[a])
                if base_post is not None and base_post >= stages["reduce_pct"]:
                    blockers.append(f"{ASSET_KO[a]} S0 사후 소진율 {base_post}% (이미 축소 임계 이상)")
                if per_bn[a] > 0 and asset_limits[a]:
                    room = asset_limits[a] * target_pct - max(0.0, base_loss - pnl_a[a])
                    caps[a] = max(0.0, room / per_bn[a])
            if per_bn_total > 0 and total_limit:
                base_loss_t = row0["loss_total_bn"] - per_bn_total * new_mv
                room_t = total_limit * target_pct - max(0.0, base_loss_t - total["pnl_bn"])
                caps["total"] = max(0.0, room_t / per_bn_total)
            if blockers:
                max_addable = {"bn": 0, "binding_asset": None, "blocked": True,
                               "rule": "추가 불가 — " + "; ".join(blockers)}
            elif caps:
                binding = min(caps, key=caps.get)
                label = "전체" if binding == "total" else ASSET_KO[binding]
                max_addable = {"bn": round(caps[binding], 0), "binding_asset": binding, "blocked": False,
                               "rule": f"S0 스트레스 후 {label} 소진율이 축소 임계({stages['reduce_pct']}%)에 닿는 규모 (자산·전체 중 작은 쪽)"}

    # ── 신호 ──
    alerts, seq = [], 0

    def _alert(grade, code, bucket, metric, value, threshold, msg, action):
        nonlocal seq
        seq += 1
        due = {"WARN": limits["deadlines"]["warn_response"],
               "REDUCE": f"D+{limits['deadlines']['reduce_days']}영업일", "HALT": "즉시"}.get(grade, "")
        alerts.append({"alert_id": f"ALT-{today.strftime('%Y%m%d')}-{seq:02d}", "code": code, "grade": grade,
                       "grade_ko": STAGE_KO[grade], "bucket": bucket,
                       "metric": {"name": metric, "value": value, "threshold": threshold, "as_of": as_of, "source": "CODE"},
                       "message": msg, "required_action": action, "due": due})

    for e in errors:
        _alert("WARN", "LIMIT_STRUCTURE", "한도 구조", "계층 검증", None, None, e, "한도표 수정(투자위)")
    action_for = {"WARN": "운용역 대응안 당일 보고", "REDUCE": "포지션 단계적 축소(규모·순서 입력)",
                  "HALT": "전량 청산·신규 금지·투자위 즉시 보고"}
    for scope, u in [("전체", total)] + [(ASSET_KO[a], by_asset[a]) for a in ASSETS]:
        if u["stage"] != "NORMAL":
            thr = stages[{"WARN": "warn_pct", "REDUCE": "reduce_pct", "HALT": "halt_pct"}[u["stage"]]]
            _alert(u["stage"], "UTIL_STAGE", scope, "한도 소진율(%)", u["util_pct"], thr,
                   f"{scope} 손실한도 소진율 {u['util_pct']}% (손실 {u['used_bn']}억/한도 {u['limit_bn']}억, 기준일 {as_of}) — {STAGE_KO[u['stage']]} 단계",
                   action_for[u["stage"]])
    for r in pos_rows:
        if r["is_new"]:
            continue
        if r["stage_asset"] != "NORMAL":
            code = "STOP_HIT" if r["stage_asset"] == "REDUCE" else "STOP_APPROACH"
            _alert(r["stage_asset"], code, r["name"], "손절 접근률(%)", r["stop_ratio_pct"],
                   100 if r["stage_asset"] == "REDUCE" else r["approach_ratio_pct"],
                   f"{r['name']} 손절선 {'도달' if r['stage_asset']=='REDUCE' else '접근'} {r['stop_ratio_pct']}% (자산손익 {r['pnl_ytd_bn']}억/손절 {r['stop_amount_bn']}억)",
                   "운용역 대응안 당일 보고" if r["stage_asset"] == "WARN" else "축소 계획 입력·기한 내 실행")
        if r["fx"] and r["fx"]["stage"] != "NORMAL":
            st = r["fx"]["stage"]
            _alert(st, "FX_STOP_HIT" if st == "REDUCE" else "FX_STOP_APPROACH", r["name"] + " [환오픈]", "환오픈 손절 접근률(%)",
                   r["fx"]["stop_ratio_pct"], 100 if st == "REDUCE" else r["approach_ratio_pct"],
                   f"{r['name']} 환오픈 손절선 {'도달' if st=='REDUCE' else '접근'} {r['fx']['stop_ratio_pct']}% (환손익 {r['fx']['pnl_fx_bn']}억/손절 {r['fx']['stop_amount_bn']}억)",
                   "헤지비율 조정안 당일 보고" if st == "WARN" else "환오픈 축소·기한 내 실행")
    messages = [f"[{a['grade_ko']}] {a['message']} → {a['required_action']} (기한 {a['due']})" for a in alerts] \
        + [f"[데이터] {w}" for w in warnings]

    # ── 리스크 등급 하한·한도 초과 (전체 + 자산별) ──
    def _floor(asset: str | None) -> str:
        parts = [total["stage"]]
        if asset:
            parts += [by_asset[asset]["stage"], by_asset[asset]["position_stage_max"]]
            if asset in ("bond", "equity") and new_position and new_position.get("currency") not in (None, "KRW"):
                parts += [by_asset["fx"]["stage"], by_asset["fx"]["position_stage_max"]]
        else:
            parts += [u["stage"] for u in by_asset.values()] + [u["position_stage_max"] for u in by_asset.values()]
        fl = _max_stage(*parts)
        if fl == "NORMAL":
            hit = any((s["post_stage_total"] in ("REDUCE", "HALT")) or (asset and s["post_stage_by_asset"][asset] in ("REDUCE", "HALT")) for s in scoring)
            fl = "WATCH" if hit else "NORMAL"
        return fl

    breach_by_asset = {a: total["stage"] in ("REDUCE", "HALT") or by_asset[a]["stage"] in ("REDUCE", "HALT") for a in ASSETS}

    return {
        "as_of": as_of, "pnl_as_of_min": min((p.get("pnl_as_of") for p in positions if p.get("pnl_as_of")), default=as_of),
        "limits_version": limits.get("meta", {}).get("version"), "limits_status": limits.get("meta", {}).get("status", ""),
        "is_sample": {"positions": is_sample("positions"), "limits": is_sample("limits")},
        "validation": {"ok": not errors, "errors": errors, "warnings": warnings},
        "total_mv_bn": round(total_mv, 1), "total": total, "by_asset": by_asset, "positions": pos_rows,
        "stress": stress, "worst_stress": worst["name"] if worst else None, "stress_refs": stress_refs,
        "suggested_cut_bn": suggested_cut, "max_addable": max_addable,
        "alerts": alerts, "messages": messages + stress_refs,
        "risk_floor": _floor(None), "floor_by_asset": {a: _floor(a) for a in ASSETS},
        "limit_breach": any(breach_by_asset.values()), "breach_by_asset": breach_by_asset,
        "stages": stages, "new_position": new_position, "source": "CODE",
    }


# ── MP 괴리 ──────────────────────────────────────────────────────
def gap_analysis(eval_result: dict, mp_by_asset: dict, band_pp: float = 2.0) -> list[dict]:
    rows_by_id = {r["id"]: r for r in eval_result["positions"]}
    out = []
    for asset, mp in (mp_by_asset or {}).items():
        if not mp:
            continue
        for t in mp.get("targets", []):
            cur = rows_by_id.get(t.get("position_id"))
            now = cur["weight_pct"] if cur else 0.0
            target = t.get("weight_pct")
            band = _f(t.get("band_pp"), band_pp)
            gap = round(now - _f(target), 2) if target is not None else None
            hedge_gap = dur_gap = None
            if t.get("hedge_pct") is not None and cur and cur.get("hedge_pct") is not None:
                hedge_gap = round(_f(cur["hedge_pct"]) - _f(t["hedge_pct"]), 1)
            if t.get("duration") is not None and cur and cur.get("duration") is not None:
                dur_gap = round(_f(cur["duration"]) - _f(t["duration"]), 2)
            status = "OUT" if ((gap is not None and abs(gap) > band) or (hedge_gap is not None and abs(hedge_gap) > 5)
                               or (dur_gap is not None and abs(dur_gap) > 0.5)) else "IN"
            out.append({"asset": asset, "position_id": t.get("position_id"), "bucket": t.get("bucket"),
                        "now_pct": round(now, 2), "mp_pct": target, "gap_pp": gap, "band_pp": band, "status": status,
                        "duration_now": cur.get("duration") if cur else None, "duration_mp": t.get("duration"), "duration_gap": dur_gap,
                        "hedge_now_pct": cur.get("hedge_pct") if cur else None, "hedge_mp_pct": t.get("hedge_pct"), "hedge_gap_pp": hedge_gap,
                        "note": t.get("note", "")})
    out.sort(key=lambda r: -(abs(r["gap_pp"] or 0) + abs(r["hedge_gap_pp"] or 0) / 5 + abs(r["duration_gap"] or 0)))
    return out


# ── 에이전트 입력용 요약 ────────────────────────────────────────
def to_markdown(res: dict) -> str:
    L = [f"### 손실한도 현황 (CODE 계산, 포지션 기준일 {res['as_of']}, 한도표 {res['limits_version']})",
         f"한도 상태: {res['limits_status']}", "",
         "| 범위 | 한도(억) | YTD 손익(억) | 손실 사용(억) | 여유(억) | 소진율(%) | 단계(소진율) | 포지션 손절 최고 |", "|---|---|---|---|---|---|---|---|",
         f"| 전체 | {res['total']['limit_bn']} | {res['total']['pnl_bn']} | {res['total']['used_bn']} | {res['total']['headroom_bn']} | {res['total']['util_pct']} | {STAGE_KO[res['total']['stage']]} | {STAGE_KO[res['total']['position_stage_max']]} |"]
    for a, u in res["by_asset"].items():
        L.append(f"| {ASSET_KO[a]} | {u['limit_bn']} | {u['pnl_bn']} | {u['used_bn']} | {u['headroom_bn']} | {u['util_pct']} | {STAGE_KO[u['stage']]} | {STAGE_KO[u['position_stage_max']]} |")
    L += ["", "| 포지션 | 자산 | 평가액(억) | 비중(%) | YTD 손익(억) | 손익률(%) | 손절선(%) | 손절 접근률(%) | 환오픈 접근률(%) | 상태 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["positions"]:
        fxr = r["fx"]["stop_ratio_pct"] if r["fx"] else "-"
        est = " (손익 추정분할)" if r.get("pnl_split_estimated") else ""
        L.append(f"| {r['name']}{est} | {ASSET_KO.get(r['asset'], r['asset'])} | {r['mv_bn']} | {r['weight_pct']} | {r['pnl_ytd_bn']} | {r['pnl_pct']} | {r['stop_loss_pct']} | {r['stop_ratio_pct']} | {fxr} | {STAGE_KO[r['stage']]}{(' · ' + r['reason']) if r['reason'] else ''}{' · 기준일 경과' if r['stale'] else ''} |")
    L += ["", "| 스트레스 | 채권 손실 | 주식 손실 | 환 손실 | 합계(억) | 후 전체 소진율(%, 순손익 상계) | 단계 |", "|---|---|---|---|---|---|---|"]
    for s in res["stress"]:
        tag = " [참고]" if s["reference"] else (" [사이징]" if s["sizing"] else "")
        L.append(f"| {s['id'] or ''} {s['name']}{tag} ({s['desc']}) | {s['loss_bn']['bond']} | {s['loss_bn']['equity']} | {s['loss_bn']['fx']} | {s['loss_total_bn']} | {s['post_util_total_pct']} | {STAGE_KO[s['post_stage_total']]} |")
    if res.get("max_addable"):
        m = res["max_addable"]
        L.append(f"\n신규 추가 가능 규모(S0 기준): " + (m["rule"] if m.get("blocked") else f"약 {m['bn']}억 — {m['rule']}"))
    if res.get("suggested_cut_bn"):
        L.append("축소 필요 규모(S0 기준, 축소 임계까지): " + ", ".join(f"{ASSET_KO[a]} 약 {v}억" for a, v in res["suggested_cut_bn"].items() if v))
    L += ["", "**발동 신호·메시지**"] + ([f"- {m}" for m in res["messages"]] or ["- 없음"])
    L.append(f"\n리스크 등급 하한(코드): 전체 {res['risk_floor']} / 자산별 " + ", ".join(f"{ASSET_KO[a]} {v}" for a, v in res["floor_by_asset"].items())
             + f" / 한도 초과(축소 이상): {'예' if res['limit_breach'] else '아니오'}")
    return "\n".join(L)


if __name__ == "__main__":
    import json
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    r = evaluate(new_position=(json.loads(sys.argv[1]) if len(sys.argv) > 1 else None))
    print(to_markdown(r))
