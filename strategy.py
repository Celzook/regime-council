# -*- coding: utf-8 -*-
"""전략 보드 — '지금 무엇을 해야 하나'를 규칙(CODE)으로 정리한다. 에이전트는 이 결과를 해석만 한다.

입력: positions.json(엑셀 투자 논리·반증 조건 포함) · limits.evaluate(손절 소진·자산별 손절 예산·스트레스) ·
quant.json(기술적 매력도·변동성·손절/목표 확률·포트 한도 초과 확률) · portfolio.json(전략 배분 목표, 가상 설정).
출력: data/strategy.json — 포지션별 할 일(+실행 수량), 전략별 현황·운용 원칙, 새 기회, 포트 한 줄.

판단 규칙(위에서부터 먼저 걸리는 것이 그 포지션의 '할 일', 나머지 해당 규칙은 '함께 볼 것'):
  손절       손익률 ≤ 손절선                                  → 전량 정리
  축소       손절 소진율 ≥ 접근 기준(한도 가안, 기본 70%)       → 절반 축소
  익절       손익률 ≥ 목표                                     → 절반 익절, 남은 절반 손절선을 진입가로
  규모 조정  손절폭 < 1개월 변동성(1σ)의 75%                    → 손절 금액은 그대로, 손절폭을 1σ로 넓히고 규모를 그만큼 줄임
  일부 익절  손익률 ≥ 목표의 70%                               → 1/3 익절, 손절선 상향
  논리 점검  기술 점수 ≤ −30 이고 1개월 안 손절 먼저 확률 ≥ 50% → 반증 조건 확인
  확대 검토  기술 점수 ≥ +30, 국면 기준 6개월 목표 확률 > 손절 확률, 자산 손절 예산 여유 있음
  유지       그 밖
매매 지시가 아니라 점검 제안이다 — 실행은 운용역, 확인은 독립 점검.

사용법: python strategy.py   # data/strategy.json 갱신 후 요약 출력
"""
import datetime
import json
import math
import os
import sys

import config
import limits
from agent import load_json

OUT = config.DATA_DIR / "strategy.json"
TECH_BAD, TECH_GOOD = -30, 30
SIGMA_FLOOR = 0.75            # 손절폭이 1개월 1σ 의 75% 미만이면 잡음에도 걸리기 쉽다
TYPICAL_STOP = {"equity": 8.0, "bond": 2.0, "mixed": 3.0, "fx": 3.0}   # 새 기회의 '최대 규모' 환산용 손절폭 가정(%)
QUANT_MAX_AGE_H = 80          # quant.json 이 이보다 오래되면 규칙에서 뺌(평일 16:30 갱신 — 금→월 72시간은 넘기지 않게)

# 순서 = 우선순위. tone 은 화면 색(bad 빨강 · warn 주황 · good 초록 · neutral 회색)
ACTIONS = {"손절": ("bad", 100), "축소": ("bad", 90), "익절": ("good", 80), "규모 조정": ("warn", 70),
           "일부 익절": ("good", 60), "논리 점검": ("warn", 50), "확대 검토": ("good", 40), "유지": ("neutral", 10)}

# 전략별 운용 원칙 — 6개월 투자시계 기준의 일반 원칙과 데스크 프롬프트의 점검 항목을 한 화면용으로 요약
PLAYBOOK = {
    "마켓타이밍": {"what": "국면·밸류·추세로 방향을 잡는 노출(롱/숏)",
               "entry": "국면 판단과 기술 점수가 같은 방향일 때 2~3회 분할 진입",
               "size": "손절폭은 1개월 변동성(1σ) 이상, 손절 금액 합계는 자산 손절 예산 안",
               "exit": "목표 도달 시 절반 익절 후 손절선을 진입가로 · 6개월 시계 끝나면 재평가"},
    "상대가치": {"what": "두 자산의 가격비·금리차가 벌어졌을 때 되돌림에 거는 롱숏",
             "entry": "가격비·금리차가 과거 분포의 극단(상·하위 10%)일 때",
             "size": "순베타(주식)·순듀레이션(채권)을 0 근처로 — 헤지 비율 월 1회 재조정",
             "exit": "분포 중앙으로 돌아오면 정리 · 구조 변화(반증 조건)면 손익과 무관하게 정리"},
    "이벤트 드리븐": {"what": "날짜가 정해진 사건(선거·배당·연초 효과·지수 편입) 전후의 가격 패턴",
                "entry": "이벤트 2~6주 전, 나가는 날짜를 먼저 정하고 진입",
                "size": "결과가 반대로 나와도 손절 안에 들어오는 규모",
                "exit": "정한 날짜에 정리 — 이벤트가 지나면 논리도 끝"},
    "자금기반 Arbitrage": {"what": "IPO 배정·CB/BW·블록딜처럼 조건·할인이 있는 물량 (목표 외 추가)",
                       "entry": "할인·전환 조건이 조달비용을 넘을 때",
                       "size": "델타 헤지로 방향성 최소화",
                       "exit": "상장·전환·할인 해소 시"},
    "위탁 대용": {"what": "위탁 운용사(EM 로컬채권 등) 확정 전까지 ETF 로 대신 잡아 둔 자리",
              "entry": "운용사 실사·선정 후 위탁으로 교체",
              "size": "운용사 확정 전까지 소규모로",
              "exit": "위탁 계약 시 이관"},
    "환 오버레이": {"what": "해외 자산은 환헤지가 원칙, 환 위험은 통화선물로만 따로 가져간다",
               "entry": "금리차·달러 사이클 판단이 설 때 통화선물로",
               "size": f"오버레이 한도 {config.STRATEGY['fx_overlay_limit_bn']:,}억 · 원화 강세 −5%·−10% 손실을 먼저 확인",
               "exit": "목표·손절 환율"},
}

# 화면용 묶음 — 할 일을 '무엇을 하나' 기준으로 4+1 묶음으로 보여 준다(쉬운 말 설명 포함)
GROUPS = [
    {"key": "reduce", "title": "줄이기", "tone": "bad", "actions": ["손절", "축소", "규모 조정"]},
    {"key": "check", "title": "다시 확인하기", "tone": "warn", "actions": ["논리 점검"]},
    {"key": "take", "title": "이익 챙기기", "tone": "good", "actions": ["익절", "일부 익절"]},
    {"key": "add", "title": "늘려 볼 만한 것", "tone": "good", "actions": ["확대 검토"]},
    {"key": "keep", "title": "그대로 두기", "tone": "neutral", "actions": ["유지"]},
]
ACTION_WHY = {
    "손절": "손절선에 닿았습니다. 정해 둔 대로 정리합니다.",
    "축소": "손절선에 가까워졌습니다. 절반을 줄여 손실을 묶어 둡니다.",
    "규모 조정": "요즘 가격이 크게 출렁여서, 지금 손절선이면 평소 흔들림만으로도 손절될 수 있습니다. "
             "금액을 줄이고 손절선을 넓히면 손절 때 잃는 돈은 같으면서 버틸 여유가 생깁니다.",
    "논리 점검": "시장 흐름이 우리가 건 방향과 반대로 가고 있습니다. 엑셀에 적어 둔 '이렇게 되면 틀린 것'(반증 조건)에 "
             "해당하는지 확인하고, 해당하면 절반을 줄입니다.",
    "익절": "목표 수익에 도달했습니다. 절반은 이익을 확정하고, 남은 절반은 손절선을 본전으로 올립니다.",
    "일부 익절": "목표에 거의 왔습니다. 1/3 은 이익을 확정하고 손절선을 올립니다.",
    "확대 검토": "흐름이 우리 편이고 손실 여유도 있습니다. 조금 더 담을지 검토합니다.",
    "유지": "특별히 할 일이 없습니다.",
}
PLAIN_NAME = {"ust_10y_cmt": "미국채 10년", "ktb_10y": "국고 10년", "usdkrw": "원/달러"}


def _flow(key: str, sc: float) -> str:
    """기술 점수의 방향을 쉬운 말로(금리·환율은 무엇이 오르는지 풀어서)."""
    strong = "뚜렷하게 " if abs(sc) >= 60 else ""
    if key in ("ust_10y_cmt", "ktb_10y"):
        return f"가격이 {strong}{'오르는(금리 하락)' if sc > 0 else '내리는(금리 상승)'} 흐름"
    if key == "usdkrw":
        return f"환율이 {strong}{'오르는(원화 약세)' if sc > 0 else '내리는(원화 강세)'} 흐름"
    return f"{strong}{'오르는' if sc > 0 else '내리는'} 흐름"


# 새 기회: 스캐너 지표 → (자산, 맞는 전략, 실행 수단)
IDEA_MAP = {
    "kospi": ("주식", "마켓타이밍", "KODEX 200(069500) · KOSPI200 선물"),
    "kosdaq": ("주식", "마켓타이밍", "KODEX 코스닥150(229200) · 코스닥150 선물"),
    "sp500": ("주식", "마켓타이밍", "SPY(환헤지) · CME E-mini S&P500"),
    "nasdaq": ("주식", "마켓타이밍", "QQQ(환헤지)"),
    "nikkei": ("주식", "마켓타이밍", "EWJ(환헤지)"),
    "stoxx50": ("주식", "마켓타이밍", "FEZ(환헤지) · KRX 유로스톡스50 선물"),
    "eem": ("주식", "마켓타이밍", "EEM(환헤지)"),
    "usdkrw": ("환", "마켓타이밍", "KRX 미국달러선물"),
    "gold": ("원자재", "마켓타이밍", "KRX 금시장 · GLD(환헤지)"),
    "wti": ("원자재", "이벤트 드리븐", "원유 선물 ETF — 변동성 큼"),
    "ktb_10y": ("채권", "마켓타이밍", "KRX 10년 국채선물"),
    "ust_10y_cmt": ("채권", "마켓타이밍", "IEF(환헤지) · 미국채 10년 선물"),
}
SHORTABLE = {"kospi", "kosdaq", "usdkrw", "ktb_10y", "ust_10y_cmt", "stoxx50", "sp500"}


def _f(v, d=0):
    return f"{v:,.{d}f}"


def _sg(v, d=1):
    r = round(v, d)
    return "0" if r == 0 else f"{r:+,.{d}f}"


def _is_fut(l: dict) -> bool:
    return "파생" in (l.get("market") or "") or " F" in str(l.get("code"))


def _leg_keys(p: dict, l: dict) -> list[tuple[str, int]]:
    """다리 → 스캐너 지표(방향). 새 기회의 '지금 보유'와 방향 충돌 판단용."""
    code, name = str(l.get("code") or ""), l.get("name") or ""
    if "KOSPI200" in code or "코스피200" in name:
        return [("kospi", 1)]
    if code in ("SPY", "IVV", "VOO"):
        return [("sp500", 1)]
    if code == "QQQ":
        return [("nasdaq", 1)]
    if code in ("IEF", "TLT", "LQD"):
        return [("ust_10y_cmt", 1)]
    if code == "EEM":
        return [("eem", 1)]
    if "LKTB" in code or "국채선물" in name:
        return [("ktb_10y", 1)]
    if code.startswith("USD F") or "미국달러선물" in name:
        return [("usdkrw", 1)]
    if code.startswith("JPY F") or "엔선물" in name:
        return [("usdkrw", 1), ("usdjpy", -1)]      # 엔/원 = 원/달러 ÷ 엔/달러
    if code.isdigit() and p.get("asset") == "equity":
        return [("kospi", 1)]
    if code.isdigit() and p.get("asset") == "bond":
        return [("ktb_10y", 1)]
    return []


def _base(p: dict) -> float:
    """포지션 기준액 — 환 오버레이는 명목, 그 밖은 진입 기준(없으면 시가). 통화와 무관하게 같은 기준."""
    return float(p.get("fx_exposure_bn") or 0) if p.get("asset") == "fx" else float(p.get("base_bn") or p.get("mv_bn") or 0)


def _exposures(positions: list[dict]) -> dict[str, list[dict]]:
    """지표별로 그 방향의 노출을 가진 포지션(베타 가중 순노출이 포지션 기준의 15% 이상)."""
    out: dict[str, list[dict]] = {}
    for p in positions:
        base = _base(p)
        net: dict[str, float] = {}
        for l in p.get("legs", []):
            sign = -1 if l.get("side") == "숏" else 1
            w = float(l.get("beta") or 1) if p.get("asset") == "equity" else 1.0
            for k, d in _leg_keys(p, l):
                net[k] = net.get(k, 0.0) + sign * d * float(l.get("mv_bn") or 0) * w
        for k, v in net.items():
            if base and abs(v) >= 0.15 * base:
                out.setdefault(k, []).append({"id": p["id"], "name": p.get("short_name") or p["name"], "dir": 1 if v > 0 else -1,
                                              "bn": round(v, 1)})
    return out


def _how(p: dict, frac: float, verb_long: str = "매도", verb_short: str = "환매(매수)") -> list[str]:
    """포지션 다리별 실행 수량. frac = 줄이는(또는 늘리는) 비율."""
    steps = []
    for l in p.get("legs", []):
        n = float(l.get("qty") or 0) * frac
        fut = _is_fut(l)
        n_txt = f"{max(1, round(n)):,}계약" if fut else f"{round(n):,}주"
        verb = verb_long if l.get("side") != "숏" else verb_short
        steps.append(f"{l['name']} {n_txt} {verb} (≈{_f(float(l.get('mv_bn') or 0) * frac)}억)")
    if len(p.get("legs", [])) > 1:
        steps.append("여러 다리는 같은 비율로 동시에 — 한쪽만 먼저 하면 그 사이 방향성 노출이 생김")
    if p.get("currency") not in (None, "KRW") and p.get("asset") != "fx" and (p.get("hedge_pct") or 0) > 0:
        steps.append(f"환헤지({p.get('hedge_pct')}%)도 같은 비율로 해제(선물환·통화선물)")
    kinds = {"fut" if _is_fut(l) else ("krx" if str(l.get("code", "")).isdigit() else "us") for l in p.get("legs", [])}
    if "krx" in kinds:
        steps.append("국내 주식·ETF: 장중 VWAP 분할, 동시호가 몰아치기 피함")
    if "us" in kinds:
        steps.append("미국 상장: 국내 시간 외 주문은 다음 미국 장 체결 — 환헤지 해제와 같은 날로 맞춤")
    if "fut" in kinds:
        steps.append("선물: 최근월물 유동성 확인, 만기 2주 안이면 롤오버 함께 판단")
    return steps


def _position_rows(doc: dict, ev: dict, q: dict | None) -> list[dict]:
    rows = {r["id"]: r for r in ev.get("positions", [])}
    qp = {r["id"]: r for r in (q or {}).get("positions", [])}
    risk = {r["id"]: r["pct"] for r in ((q or {}).get("portfolio") or {}).get("risk_contrib", [])}
    scan = {s["key"]: s for s in (q or {}).get("scanner", [])}
    against: dict[str, list[tuple]] = {}          # 포지션 → 보유 방향과 반대인 강한 시장 신호 (key, 점수)
    for k, hs in _exposures(doc.get("positions", [])).items():
        sc = (scan.get(k) or {}).get("score")
        if sc is None or abs(sc) < TECH_GOOD:
            continue
        for h in hs:
            if h["dir"] * sc < 0:
                against.setdefault(h["id"], []).append((k, sc))
    by_asset = ev.get("by_asset", {})
    overlay_used = sum(float(p.get("fx_exposure_bn") or 0) for p in doc.get("positions", []) if p.get("asset") == "fx")
    out = []
    for p in doc.get("positions", []):
        r = rows.get(p["id"], {})
        is_fx = p.get("asset") == "fx"
        fx = r.get("fx") or {}
        base = _base(p)
        mv_now = base if is_fx else float(p.get("mv_bn") or base)   # 주문 금액은 지금 시가 기준(수량 계산과 같은 기준)
        pnl = float(p.get("pnl_ytd_bn") or 0) + float(p.get("pnl_fx_ytd_bn") or 0)
        ret = pnl / base * 100 if base else 0.0
        stop = p.get("fx_stop_pct") if is_fx else p.get("stop_loss_pct")
        target = p.get("target_pct")
        used = float((fx.get("stop_ratio_pct") if is_fx else r.get("stop_ratio_pct")) or 0)
        approach = float(r.get("approach_ratio_pct") or 70)
        qq = qp.get(p["id"]) or {}
        tech = (qq.get("tech") or {}).get("score")
        tlabel = (qq.get("tech") or {}).get("label")
        v1, a6 = (qq.get("vol") or {}).get("1M") or {}, (qq.get("analog") or {}).get("6M") or {}
        sig1m = float(v1["sigma_ann_pct"]) * math.sqrt(21 / 252) if v1.get("sigma_ann_pct") else None
        asset = p.get("asset")
        if is_fx:
            room_bn = max(0.0, float(config.STRATEGY.get("fx_overlay_limit_bn", 1000)) - overlay_used)
            room_txt = f"오버레이 한도 여유 {_f(room_bn)}억(명목)"
        else:
            ba = by_asset.get("bond" if asset == "mixed" else asset, {})
            room_bn = float(ba.get("stop_budget_free_bn") or 0)
            room_txt = f"{'주식' if asset == 'equity' else '채권'} 손절 예산 여유 {_f(room_bn)}억"

        hits = []   # (action, headline, frac, extra_steps)
        plain: dict[str, tuple[str, str]] = {}    # 할 일 → (무엇을, 왜) 쉬운 말
        if stop is not None and ret <= stop:
            plain["손절"] = (f"전량 정리 · {_f(mv_now)}억", f"손실 {ret:.1f}%로 손절선 −{abs(stop):g}%에 닿음")
            hits.append(("손절", f"손절선({stop}%)에 닿음 — 전량 정리", 1.0, ["정리 후 투자위 보고(손절 실행 기록)"]))
        if used >= approach and not (stop is not None and ret <= stop):
            hits.append(("축소", f"손절선의 {used:.0f}%까지 옴(접근 기준 {approach:.0f}%) — 절반 축소", 0.5, []))
            plain["축소"] = (f"절반 줄이기 · {_f(mv_now * 0.5)}억", f"손절선까지 갈 손실의 {used:.0f}%를 이미 잃음")
        if target and ret >= target:
            hits.append(("익절", f"목표 {target}% 도달 — 절반 익절", 0.5, ["남은 절반의 손절선을 진입가(0%)로 올림"]))
            plain["익절"] = (f"절반 이익 실현 · {_f(mv_now * 0.5)}억", f"수익 {ret:+.1f}%로 목표 {target:g}% 도달")
        if stop and sig1m and abs(stop) < SIGMA_FLOOR * sig1m:
            new_stop = math.ceil(sig1m * 2) / 2                          # 0.5%p 단위로 올림
            keep = 1 - abs(stop) / new_stop
            if keep >= 0.1:
                hits.append(("규모 조정",
                             f"손절폭 {abs(stop):g}%가 한 달 변동성(1σ) {sig1m:.1f}%보다 좁아 잡음에도 걸리기 쉬움 — "
                             f"규모 {keep * 100:.0f}% 줄이고 손절폭 {new_stop:g}%로",
                             keep, [f"손절선을 −{new_stop:g}%로 재설정 — 손절 금액 {_f(base * abs(stop) / 100)}억은 그대로 유지"]))
                plain["규모 조정"] = (f"{_f(mv_now * keep)}억 줄이기 ({keep * 100:.0f}%) · 손절선 −{abs(stop):g}% → −{new_stop:g}%",
                                    f"요즘 한 달에 ±{sig1m:.0f}% 정도 출렁이는데 손절선은 −{abs(stop):g}%")
        if target and 0.7 * target <= ret < target:
            hits.append(("일부 익절", f"목표의 {ret / target * 100:.0f}%까지 옴 — 1/3 익절, 손절선 상향", 1 / 3,
                         [f"손절선을 {stop / 2 if stop else 0:g}% 이상으로 올림"]))
            plain["일부 익절"] = (f"1/3 이익 실현 · {_f(mv_now / 3)}억", f"수익 {ret:+.1f}% — 목표 {target:g}%의 {ret / target * 100:.0f}%")
        if tech is not None and tech <= TECH_BAD and (v1.get("p_stop") or 0) >= 50:
            hits.append(("논리 점검", f"추세가 반대(기술 점수 {tech:+.0f})이고 한 달 안 손절 먼저 확률 {v1.get('p_stop')}% — 반증 조건 확인",
                         0.0, [f"반증 조건: {p.get('invalidation') or '(엑셀에 미기재)'}", "해당하면 '축소'(절반)로 전환"]))
            plain["논리 점검"] = ("반증 조건에 해당하는지 확인 → 해당하면 절반 줄이기",
                               f"이 포지션이 내리는 흐름이고, 한 달 안에 손절선부터 닿을 확률 {v1.get('p_stop')}%")
        if (tech is not None and tech >= TECH_GOOD and (a6.get("p_target") or 0) > (a6.get("p_stop") or 0)
                and room_bn > 0 and not is_fx):
            add_bn = min(0.25 * base, room_bn / (abs(stop) / 100)) if stop else 0.25 * base
            if add_bn >= 10:
                hits.append(("확대 검토", f"추세 우호(기술 점수 {tech:+.0f}) · 국면 기준 6개월 목표 {a6.get('p_target')}% > 손절 {a6.get('p_stop')}% — "
                                      f"여유 안에서 최대 {_f(add_bn)}억 추가 검토", add_bn / base if base else 0, [room_txt]))
                plain["확대 검토"] = (f"최대 {_f(add_bn)}억 더 담기 검토", "오르는 흐름이고 손실 여유도 있음")
        if not hits:
            gap_s = f"손절까지 {ret - stop:.1f}%p" if stop is not None else "손절 미설정"
            gap_t = f"목표까지 {target - ret:.1f}%p" if target else "목표 미설정"
            hits.append(("유지", f"논리·한도 이상 없음 — {gap_s}, {gap_t}", 0.0, []))
            plain["유지"] = (f"할 일 없음" + (f" · 손절선까지 {ret - stop:.1f}%p" if stop is not None else ""),
                           f"손익 {ret:+.1f}%" + (f" · 목표까지 {target - ret:.1f}%p" if target else ""))
        if against.get(p["id"]) and p.get("strategy") != "상대가치":   # 페어는 다리별 방향이 논리가 아님
            sig_txt = "시장 신호가 보유 방향과 반대 — " + ", ".join(f"{scan[k]['name']} 기술 {sc:+.0f}" for k, sc in against[p["id"]])
            i = next((i for i, h in enumerate(hits) if h[0] == "논리 점검"), None)
            if i is None:
                hits.append(("논리 점검", sig_txt + " · 반증 조건 확인", 0.0, [f"반증 조건: {p.get('invalidation') or '(엑셀에 미기재)'}"]))
            else:   # 추세 기준 논리 점검이 이미 있으면 한 줄로 합침(중복 표시 방지)
                hits[i] = (hits[i][0], hits[i][1] + " · " + sig_txt, hits[i][2], hits[i][3])
            k0, sc0 = against[p["id"]][0]
            plain.setdefault("논리 점검", ("반증 조건에 해당하는지 확인 → 해당하면 절반 줄이기",
                                        f"{PLAIN_NAME.get(k0, scan[k0]['name'])} {_flow(k0, sc0)}인데, 우리는 {'오를' if sc0 < 0 else '내릴'} 때 버는 쪽"))
            hits = [h for h in hits if h[0] != "유지"] or hits
        hits.sort(key=lambda h: -ACTIONS[h[0]][1])     # 시장 신호까지 붙인 뒤 정렬 — 우선순위 높은 규칙이 할 일
        act, head, frac, extra = hits[0]
        tone, prio = ACTIONS[act]
        do, why = plain.get(act, (head, ""))
        if act in ("손절", "축소", "익절", "규모 조정", "일부 익절"):
            how = _how(p, frac) + extra
        elif act == "확대 검토":
            how = _how(p, frac, verb_long="추가 매수", verb_short="추가 매도") + extra
        else:
            how = extra or ["지금은 거래 없음 — 다음 일일 점검에서 다시 판단"]
        # 근거 숫자 — 화면에 그대로 나가므로 쉬운 말로
        facts = [f"손익 {_sg(ret)}% ({_sg(pnl)}억)"]
        if stop is not None:
            facts.append(f"손절선 −{abs(stop):g}% · 그중 이미 잃은 비율 {used:.0f}%")
        if target:
            facts.append(f"목표 {_sg(target, 1)}%")
        if sig1m:
            facts.append(f"한 달 출렁임 ±{sig1m:.1f}%")
        if tech is not None:
            facts.append(f"흐름 점수 {tech:+.0f} ({tlabel})")
        if v1.get("p_stop") is not None:
            facts.append(f"한 달 안에 손절선 먼저 {v1['p_stop']:g}% · 목표 먼저 {v1.get('p_target'):g}%")
        if a6.get("p_stop") is not None:
            facts.append(f"과거 비슷한 시기 6개월: 손절선 {a6['p_stop']:g}% · 목표 {a6.get('p_target'):g}%")
        out.append({
            "id": p["id"], "name": p.get("short_name") or p["name"], "full_name": p["name"],
            "asset": asset, "strategy": "환 오버레이" if is_fx else (p.get("strategy") or "기타"),
            "base_bn": round(base, 1), "pnl_bn": round(pnl, 2), "ret_pct": round(ret, 2),
            "stop_pct": stop, "target_pct": target, "stop_used_pct": round(used, 1),
            "sigma_1m_pct": round(sig1m, 2) if sig1m else None, "tech": tech,
            "action": act, "tone": tone, "priority": prio, "headline": head, "do": do, "why": why,
            "impact_bn": round(mv_now * frac, 1) if act not in ("유지", "논리 점검") else 0.0,
            "how": how, "facts": facts, "also": [{"action": h[0], "headline": h[1]} for h in hits[1:]],
            "thesis": p.get("thesis"), "invalidation": p.get("invalidation"), "evidence": p.get("evidence"),
            "room": room_txt, "risk_pct": risk.get(p["id"]),
        })
    # 같은 할 일이면 포트 위험 기여가 큰 것부터(금액이 아니라 위험이 큰 순서)
    out.sort(key=lambda x: (-x["priority"], -(x["risk_pct"] or 0), -abs(x["impact_bn"]), x["id"]))
    return out


def _concentration(doc: dict) -> list[str]:
    """같은 종목이 여러 포지션에 같은 방향으로 걸린 경우(예: 방향성 롱 + 페어 롱)."""
    seen: dict[tuple, list] = {}
    for p in doc.get("positions", []):
        for l in p.get("legs", []):
            seen.setdefault((str(l.get("code")), l.get("side")), []).append((p["id"], l["name"], float(l.get("mv_bn") or 0)))
    out = []
    for (code, side), xs in seen.items():
        if len({x[0] for x in xs}) > 1:
            out.append(f"{xs[0][1]} {side} {_f(sum(x[2] for x in xs))}억이 {'·'.join(x[0] for x in xs)} 에 나뉘어 있음 — 합산 노출로 관리")
    return out


def _strategies(acts: list[dict], q: dict | None, pf: dict) -> list[dict]:
    mix = (pf.get("book") or {}).get("strategy_mix_pct", {})
    tgt = {"마켓타이밍": mix.get("market_timing"), "상대가치": mix.get("relative_value"), "이벤트 드리븐": mix.get("event_driven")}
    risk = {r["id"]: r["pct"] for r in ((q or {}).get("portfolio") or {}).get("risk_contrib", [])}
    long_total = sum(a["base_bn"] for a in acts if a["asset"] != "fx")
    order = ["마켓타이밍", "상대가치", "이벤트 드리븐", "자금기반 Arbitrage", "위탁 대용", "환 오버레이"]
    keys = order + sorted({a["strategy"] for a in acts} - set(order))
    out = []
    for k in keys:
        xs = [a for a in acts if a["strategy"] == k]
        if not xs:
            continue
        mv = sum(a["base_bn"] for a in xs)
        cnt: dict[str, int] = {}
        for a in xs:
            cnt[a["action"]] = cnt.get(a["action"], 0) + 1
        rsk = round(sum(risk.get(a["id"], 0) for a in xs), 1)
        pnl = round(sum(a["pnl_bn"] for a in xs), 1)
        todo = [f"{n} {c}" for n, c in sorted(cnt.items(), key=lambda kv: -ACTIONS[kv[0]][1]) if n != "유지"]
        cur = round(mv / long_total * 100, 1) if long_total and k != "환 오버레이" else None
        now = []
        if todo:
            now.append("할 일 " + " · ".join(todo))
        if rsk >= 40:
            now.append(f"포트 위험의 {rsk:.0f}%가 이 전략에 몰림")
        if tgt.get(k) and cur is not None and abs(cur - tgt[k]) >= 10:
            now.append(f"배분 {cur:.0f}% — 목표 {tgt[k]}%와 {cur - tgt[k]:+.0f}%p 차이")
        out.append({"key": k, "playbook": PLAYBOOK.get(k, {}), "target_pct": tgt.get(k), "current_pct": cur,
                    "mv_bn": round(mv, 1), "pnl_bn": pnl, "risk_pct": rsk, "counts": cnt,
                    "positions": [{"id": a["id"], "name": a["name"], "action": a["action"], "tone": a["tone"],
                                   "ret_pct": a["ret_pct"], "asset": a["asset"]} for a in xs],
                    "now": " · ".join(now) or "이상 없음 — 원칙대로 유지"})
    return out


def _ideas(q: dict | None, doc: dict, ev: dict) -> list[dict]:
    expo = _exposures(doc.get("positions", []))
    by_asset = ev.get("by_asset", {})
    room = {"주식": float(by_asset.get("equity", {}).get("stop_budget_free_bn") or 0),
            "채권": float(by_asset.get("bond", {}).get("stop_budget_free_bn") or 0)}
    out = []
    for s in (q or {}).get("scanner", []):
        sc, med, up = s.get("score"), s.get("analog_6m_median_pct"), s.get("analog_6m_up_pct")
        if sc is None or s["key"] not in IDEA_MAP:
            continue
        strong = abs(sc) >= TECH_GOOD
        agree = med is not None and ((sc > 0 and med > 0 and (up or 0) >= 55) or (sc < 0 and med < 0 and (up or 100) <= 45))
        if not strong:
            continue
        asset, strat, inst = IDEA_MAP[s["key"]]
        d = 1 if sc > 0 else -1
        held = expo.get(s["key"], [])
        same = [h for h in held if h["dir"] == d]
        against = [h for h in held if h["dir"] == -d]
        if against:
            act = "보유와 반대 신호 — 해당 포지션 점검"
        elif same:
            act = "보유 중 — 유지·확대 검토"          # 이미 같은 방향(숏 포함)으로 보유 — 아래 '그대로 유지' 설명과 맞춤
        elif d > 0:
            act = "롱 검토"
        else:
            act = "선물 숏 검토" if s["key"] in SHORTABLE else "편입 보류"
        akey = {"주식": "equity", "채권": "bond", "환": "fx"}.get(asset)
        cap = None
        if asset in room:
            cap = room[asset] / (TYPICAL_STOP[akey] / 100) if akey else None
        nm = PLAIN_NAME.get(s["key"], s["name"])
        ids = "·".join(h["id"] for h in (against or same))
        if against:
            say = f"우리 {ids}는 반대 방향 → 위 '다시 확인하기'에서 점검"
        elif same:
            say = f"이미 {ids}로 보유 중 — 그대로 유지"
        elif d > 0:
            say = f"살 만한 후보 ({inst.split(' · ')[0]})"
            if asset in room and room[asset] < 50:
                say += f" — 다만 {asset} 손실 여유가 {_f(room[asset])}억뿐이라 다른 것을 줄인 뒤에"
        else:
            say = (f"하락에 거는 선물 숏도 검토 가능 ({inst.split(' · ')[-1]})" if s["key"] in SHORTABLE else "지금은 보류")
        subj = "가격이 " if s["key"] in ("ust_10y_cmt", "ktb_10y") else ("환율이 " if s["key"] == "usdkrw" else "")
        past = (None if med is None else
                f"과거 비슷한 시기엔 6개월 뒤 {subj}{'오른' if med > 0 else '내린'} 경우가 많았음({up if med > 0 else 100 - (up or 0)}%)"
                + ("" if agree else " — 지금 흐름과 엇갈림"))
        out.append({
            "plain_name": nm, "flow": _flow(s["key"], sc), "say": say, "past": past,
            "key": s["key"], "name": s["name"], "score": sc, "label": s.get("label"), "as_of": s.get("as_of"),
            "analog_6m_median_pct": med, "analog_6m_up_pct": up, "agree": agree,
            "asset": asset, "strategy": strat, "instrument": inst, "direction": "상승" if d > 0 else "하락",
            "action": act, "conflict": bool(against),
            "held": [f"{h['id']} {h['name']} ({'롱' if h['dir'] > 0 else '숏'} {_f(abs(h['bn']))}억)" for h in held],
            "room": (f"{asset} 손절 예산 여유 {_f(room[asset])}억 → 손절폭 {TYPICAL_STOP[akey]:g}% 가정 시 최대 {_f(cap)}억"
                     if asset in room else ("오버레이 한도 안에서" if asset == "환" else "Active 범위 밖 — 투자위 확인")),
            "tech": {k: s.get(k) for k in ("score", "label", "trend", "mom6", "mom1", "rsi", "ret_1m", "ret_3m", "ret_6m", "vol_ann", "dd_1y")},
        })
    out.sort(key=lambda x: (not x["conflict"], not x["agree"], -abs(x["score"])))
    return out


def _portfolio(acts: list[dict], q: dict | None, ev: dict, doc: dict) -> dict:
    pf = (q or {}).get("portfolio") or {}
    vol, an = pf.get("vol") or {}, pf.get("analog") or {}
    names = {a["id"]: a["name"] for a in acts}
    top = (pf.get("risk_contrib") or [])[:3]
    scale = (vol.get("scale_for_breach") or {}).get("20")
    room = pf.get("room_bn", pf.get("limit_bn") or 0)       # 남은 한도 = 한도 + 현재 손익(예전 quant.json 은 한도 그대로)
    over = pf.get("room_bn") is not None and room <= 0      # 이미 한도 초과 — scale_for_breach 는 0, 줄일 비율로 낮출 수 없음
    red = (f" · 축소 단계(한도 {pf.get('reduce_pct', 75):g}%) 도달 변동성 기준 {vol['p_reduce']}% · 국면 기준 {an.get('p_reduce', '-')}%"
           if vol.get("p_reduce") is not None else "")
    lines = []
    if vol.get("p_breach") is not None:
        head = (f"손실한도 이미 초과 — 남은 한도 없음(한도 {_f(pf.get('limit_bn') or 0)}억, 현재 손익 {_sg(pf.get('pnl_now_bn') or 0, 0)}억, "
                f"초과 {_f(abs(room))}억). 6개월 안에 한도를 넘어 있을 확률: " if over else
                f"6개월 안에 남은 손실한도 {_f(room)}억(한도 {_f(pf.get('limit_bn') or 0)}억, 현재 손익 {_sg(pf.get('pnl_now_bn') or 0, 0)}억)을 넘을 확률: ")
        lines.append(head + f"변동성 기준 {vol['p_breach']}% · 국면 기준 {an.get('p_breach', '-')}%" + red)
        if over:
            lines.append("이미 한도를 넘어 노출 비율 조정으로는 20%까지 낮출 수 없음 — 추가 위험 금지, 위험 기여 상위부터 줄여 한도 안으로")
        elif scale and scale < 1 and vol["p_breach"] > 20:
            lines.append(f"변동성 기준 20%로 낮추려면 전체 노출을 지금의 {scale * 100:.0f}% 로 — 위험 기여 상위부터 줄이는 것이 효율적")
    if top:
        lines.append("위험 기여 상위: " + " · ".join(f"{t['id']} {names.get(t['id'], '')} {t['pct']}%" for t in top)
                     + f" (합 {sum(t['pct'] for t in top):.0f}%)")
    ba = ev.get("by_asset", {})
    budget = []
    for k, ko in (("equity", "주식"), ("bond", "채권")):
        b = ba.get(k) or {}
        if b.get("limit_bn"):
            used = float(b.get("stop_budget_used_bn") or 0)
            budget.append({"asset": ko, "limit_bn": b["limit_bn"], "used_bn": round(used, 1), "free_bn": round(float(b.get("stop_budget_free_bn") or 0), 1),
                           "used_pct": round(used / b["limit_bn"] * 100, 1)})
    for b in budget:
        if b["used_pct"] >= 90:
            lines.append(f"{b['asset']} 손절 예산 {b['used_pct']:.0f}% 사용 — 새 {b['asset']} 포지션은 기존을 줄이거나 손절을 좁혀야 들어갈 수 있음")
    scoring = [s for s in ev.get("stress", []) if not s.get("sizing") and not s.get("reference")]
    worst = max(scoring, key=lambda s: s["loss_total_bn"]) if scoring else None
    if worst:
        lines.append(f"최악 스트레스 '{worst['name']}': 손실 {_f(worst['loss_total_bn'])}억 → 전체 한도 소진 {worst.get('post_util_total_pct')}%")
    tone = "bad" if (vol.get("p_breach") or 0) >= 40 or (an.get("p_breach") or 0) >= 25 else ("warn" if (vol.get("p_breach") or 0) >= 20 else "good")
    tiles = []
    pb = vol.get("p_breach")
    if pb is not None:
        tiles.append({"label": f"손실한도 이미 초과({_f(abs(room))}억 넘음)" if over else f"6개월 안에 남은 손실한도({_f(room)}억)를 넘을 가능성",
                      "value": "초과" if over else ("높음" if pb >= 40 else ("보통" if pb >= 20 else "낮음")),
                      "tone": "bad" if over or pb >= 40 else ("warn" if pb >= 20 else "good"),
                      "sub": f"최근 흔들림 기준 {pb:g}% · 과거 비슷한 시기 기준 {an.get('p_breach', '-')}%"
                             + (f" · 축소 단계 도달 {vol['p_reduce']:g}%" if vol.get("p_reduce") is not None else "")})
    if top:
        tiles.append({"label": "상위 3개 포지션에 몰린 위험", "value": f"{sum(t['pct'] for t in top):.0f}%",
                      "tone": "bad" if sum(t["pct"] for t in top) >= 60 else "", "sub": " · ".join(names.get(t["id"], t["id"]) for t in top)})
    eqb = next((b for b in budget if b["asset"] == "주식"), None)
    if eqb:
        free_pct = 100 - eqb["used_pct"]
        tiles.append({"label": "주식을 더 살 여유", "value": "거의 없음" if free_pct < 10 else ("적음" if free_pct < 30 else "있음"),
                      "tone": "bad" if free_pct < 10 else ("warn" if free_pct < 30 else "good"),
                      "sub": f"손실 여유 {_f(eqb['free_bn'])}억 / {_f(eqb['limit_bn'])}억"})
    if worst:
        tiles.append({"label": f"'{worst['name']}' 같은 충격이 오면", "value": f"한도의 {worst.get('post_util_total_pct', 0):.0f}%",
                      "tone": "bad" if (worst.get("post_util_total_pct") or 0) >= 100 else "", "sub": f"손실 약 {_f(worst['loss_total_bn'])}억"})
    lead = {"bad": "지금 포트는 위험이 큽니다", "warn": "지금 포트는 주의가 필요합니다", "good": "지금 포트는 안정적입니다"}[tone]
    sub = ""
    if top and sum(t["pct"] for t in top) >= 60:
        sub = (f"위험의 {sum(t['pct'] for t in top):.0f}%가 {'·'.join(names.get(t['id'], t['id']) for t in top)}에 몰려 있어 이 셋부터 줄이는 게 가장 효과적입니다.")
    if over:
        sub += " 이미 손실한도를 넘었습니다 — 새 위험은 더하지 말고, 위험 기여 상위부터 줄여 한도 안으로 되돌려야 합니다."
    elif scale and scale < 1 and (pb or 0) > 20:
        sub += f" 한도를 넘을 가능성을 '보통'(20%)으로 낮추려면 전체 규모를 지금의 {scale * 100:.0f}% 정도로 줄여야 합니다."
    return {"tone": tone, "lead": lead, "sub": sub.strip(), "tiles": tiles,
            "lines": lines, "p_breach_vol": vol.get("p_breach"), "p_breach_analog": an.get("p_breach"),
            "p_reduce_vol": vol.get("p_reduce"), "p_reduce_analog": an.get("p_reduce"), "room_bn": pf.get("room_bn"),
            "worst": {"name": worst["name"], "loss_bn": worst["loss_total_bn"], "util_pct": worst.get("post_util_total_pct")} if worst else None,
            "scale_for_20": scale, "risk_top": [{**t, "name": names.get(t["id"], "")} for t in top],
            "stop_budget": budget, "concentration": _concentration(doc)}


def _quant_fresh(meta: dict) -> tuple[dict | None, str | None, str | None]:
    """(쓸 quant 결과, 뺐다면 그 계산 시각, 사유). 오래됐거나(QUANT_MAX_AGE_H) 다른 포지션 구성으로 계산된 결과는 규칙에 쓰지 않는다.
    구성 비교는 양쪽에 positions_key(book.write_positions 의 구성 지문)가 있으면 그것으로, 없으면 포지션 기준일(as_of)로."""
    import quant
    q = quant.load()
    if not q:
        return None, None, None
    at = q.get("generated_at") or ""
    try:
        age_h = (datetime.datetime.now() - datetime.datetime.fromisoformat(at)).total_seconds() / 3600
    except ValueError:
        age_h = float("inf")
    if age_h > QUANT_MAX_AGE_H:
        return None, at, f"정량 결과({at[:16]})가 {QUANT_MAX_AGE_H}시간보다 오래돼"
    if q.get("positions_key") and meta.get("positions_key"):      # 구성 지문 — 시가 재평가(as_of 만 바뀜)로는 달라지지 않음
        if q["positions_key"] != meta["positions_key"]:
            return None, at, "정량 결과가 지금과 다른 포지션 구성으로 계산돼"
    elif q.get("positions_as_of") != meta.get("as_of"):              # 지문이 없는 예전 파일은 기준일로 비교
        return None, at, f"정량 결과의 포지션 기준일({q.get('positions_as_of')})이 지금 포지션({meta.get('as_of')})과 달라"
    return q, None, None


def build(save: bool = True) -> dict:
    import quant
    doc = limits.load_positions()
    meta = doc.get("meta", {})
    ev = limits.evaluate(positions_doc=doc, limits=limits.load_limits())
    q, stale_at, stale_why = _quant_fresh(meta)
    pf = load_json(config.DATA_DIR / "portfolio.json") or {}
    acts = _position_rows(doc, ev, q)
    port = _portfolio(acts, q, ev, doc)
    if stale_why:
        port["lines"].insert(0, f"{stale_why} 흐름 점수·확률 규칙(논리 점검·확대 검토·규모 조정·한도 초과 확률·새 기회)은 뺐음 — "
                             "전략 탭 '다시 계산'(전략 재계산)을 누르면 다시 들어감(또는 python quant.py)")
    cnt: dict[str, int] = {}
    for a in acts:
        cnt[a["action"]] = cnt.get(a["action"], 0) + 1
    todo = [a for a in acts if a["action"] != "유지"]
    groups = []
    for g in GROUPS:
        xs = [a for a in acts if a["action"] in g["actions"]]
        if xs:
            acts_in = sorted({a["action"] for a in xs}, key=lambda k: -ACTIONS[k][1])
            groups.append({**{k: g[k] for k in ("key", "title", "tone")}, "n": len(xs), "ids": [a["id"] for a in xs],
                           "why": " ".join(ACTION_WHY[k] for k in acts_in),
                           "total_bn": round(sum(a["impact_bn"] for a in xs), 0)})
    gn = {g["key"]: g["n"] for g in groups}
    asks = [f"{gn[k]}개는 {w}" for k, w in (("reduce", "줄이고"), ("check", "다시 확인하고"), ("take", "이익을 챙기고"), ("add", "늘려 볼 만합니다"))
            if gn.get(k)]
    verdict = port["lead"] + (" — " + ", ".join(asks).rstrip("고") + ("세요." if not asks[-1].endswith("니다") else "") if asks else ". 지금은 원칙대로 유지하면 됩니다.")
    d = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "positions_as_of": meta.get("as_of"), "quant_at": (q or {}).get("generated_at"),
        "quant_stale_at": stale_at, "quant_stale_reason": stale_why,
        "is_trial": quant.is_trial(meta), "is_sample": limits.is_sample("positions"),
        "summary": {"todo": len(todo), "counts": [{"action": k, "n": v, "tone": ACTIONS[k][0]}
                                                  for k, v in sorted(cnt.items(), key=lambda kv: -ACTIONS[kv[0]][1])],
                    "headline": (f"오늘 할 일 {len(todo)}건 — 먼저 {todo[0]['name']}: {todo[0]['action']}" if todo else "오늘은 원칙대로 유지")},
        "verdict": verdict, "groups": groups,
        "portfolio": port, "actions": acts, "strategies": _strategies(acts, q, pf), "ideas": _ideas(q, doc, ev),
        "rules": [   # 화면 맨 아래 '어떻게 판단하나요?' — 쉬운 말
            "손절선에 닿으면 → 전부 정리",
            "손절선까지 갈 손실의 70% 이상을 이미 잃었으면 → 절반 줄이기",
            "목표 수익에 닿으면 → 절반 이익 실현, 남은 절반은 손절선을 본전으로",
            "손절선이 한 달 출렁임의 75%보다 좁으면 → 손절 때 잃는 돈은 그대로 두고, 손절선을 한 달 출렁임만큼 넓히는 대신 금액을 줄이기",
            "목표의 70%까지 왔으면 → 1/3 이익 실현, 손절선 올리기",
            "흐름 점수 −30 이하이고 한 달 안에 손절선부터 닿을 확률이 50% 이상이거나, 시장 흐름이 우리 방향과 뚜렷하게 반대면(페어 제외) → 반증 조건 다시 확인",
            "흐름 점수 +30 이상이고, 과거 비슷한 시기 6개월에 목표가 손절보다 먼저 닿은 경우가 많고, 손실 여유가 있으면 → 최대 25% 더 담기 검토",
            "흐름 점수 = 추세·최근 수익률·과열 정도를 합친 −100~+100 점수. 시장 기회는 ±30 이상만 보여 줌",
            "같은 묶음 안에서는 포트 위험을 많이 차지하는 것부터",
        ],
        "note": "규칙 기반 점검 제안(CODE) — 매매 지시가 아님. 실행은 운용역, 확인은 독립 점검. "
                "수량은 지금 시가 기준이며 거래비용·체결 영향·대차 가능 여부는 반영하지 않음.",
    }
    if save:
        tmp = OUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, OUT)
    return d


def to_markdown(d: dict) -> str:
    """에이전트 컨텍스트용 요약(숫자는 CODE 계산)."""
    tag = " · 가상 시험 포트" if d.get("is_trial") else (" · 샘플(계획치)" if d.get("is_sample") else "")
    L = [f"## 전략 보드 (CODE · {d['generated_at'][:16]}{tag})",
         d["summary"]["headline"], ""]
    L += [f"- {x}" for x in d["portfolio"]["lines"] + d["portfolio"]["concentration"]]
    L += ["", "| 포지션 | 할 일 | 이유 | 영향(억) |", "|---|---|---|---|"]
    for a in d["actions"]:
        L.append(f"| {a['id']} {a['name']} | {a['action']} | {a['headline']} | {a['impact_bn']} |")
    ideas = d["ideas"][:5]
    if ideas:
        L += ["", "새 기회(스캐너): " + " / ".join(f"{i['name']} {i['direction']}(기술 {i['score']:+.0f}) → {i['action']}" for i in ideas)]
    L.append("규칙: " + " · ".join(d["rules"][:7]))
    return "\n".join(L)


def load(max_age_hours: float | None = None) -> dict | None:
    d = load_json(OUT)
    if d and max_age_hours is not None:
        age = datetime.datetime.now() - datetime.datetime.fromisoformat(d["generated_at"])
        if age.total_seconds() > max_age_hours * 3600:
            return None
    return d


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(to_markdown(build()))
