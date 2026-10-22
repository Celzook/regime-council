# -*- coding: utf-8 -*-
"""시장 브리프 (자동 리포트).

- CODE: analytics.py 의 주목 신호·지표 표. API 키 없이 항상 생성 → reports/YYYY-MM-DD_시장지표.md
- AGENT: API 키가 있으면 BOND·STOCK·FX 가 데스크 섹션을 병렬로 쓰고 CIO 종합을 붙인다(섹션마다 에이전트 그림)
  (형식은 prompts/market_brief.md) → reports/YYYY-MM-DD_시장브리프.md
  키가 없을 때 같은 형식의 에이전트 브리프는 Claude Code 의 market-desk 스킬이 만든다.
"""
import datetime
import os
from concurrent.futures import ThreadPoolExecutor

import config
import store

DESK_INSTRUCTION = "시장 브리프의 'A. 데스크 섹션'을 작성하라. 이름은 '{name}', 데스크는 '{desk}', 오늘 날짜는 {day}."


def avatar_tag(asset: str, size: int = 44) -> str:
    """브리프 마크다운에 넣는 에이전트 그림 — 대시보드 보고서 뷰어 기준 경로(static/avatars)."""
    p = config.AGENT_PERSONAS[asset]
    return f'<img src="{p["avatar"]}" width="{size}" height="{size}" alt="{p["name"]}">'
CIO_INSTRUCTION = "시장 브리프의 'B. CIO 종합'을 작성하라. 오늘 날짜는 {day}.\n\n# 데스크 섹션\n\n{desks}"


def _agent_sections(day: str, model: str | None) -> tuple[dict, str | None, list[str]]:
    import anthropic

    from agent import Agent, build_shared_context, load_prompt

    client = anthropic.Anthropic()
    shared = build_shared_context()
    guide = load_prompt("market_brief")

    def desk(asset: str) -> str:
        ag = Agent(asset, shared, client, model=model, extra_system=guide)
        p = config.AGENT_PERSONAS[asset]
        return ag.answer(DESK_INSTRUCTION.format(name=p["name"], desk=p["desk"], day=day), mode="discuss")

    desks, errors = {}, []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {a: pool.submit(desk, a) for a in config.AGENTS}
    for a, f in futs.items():
        try:
            desks[a] = f.result()
        except Exception as e:  # noqa: BLE001 — 한 데스크 실패가 브리프 전체를 막지 않게
            errors.append(f"{config.AGENT_NAMES[a]} 실패: {type(e).__name__}: {e}")
    cio = None
    if desks:
        try:
            with client.messages.stream(
                model=model or config.MODEL,
                max_tokens=config.MAX_TOKENS["synthesis"],
                output_config={"effort": config.EFFORT["synthesis"]},
                system=[{"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": guide}],
                messages=[{"role": "user", "content": CIO_INSTRUCTION.format(day=day, desks="\n\n".join(desks.values()))}],
            ) as stream:
                msg = stream.get_final_message()
            cio = next((b.text for b in msg.content if b.type == "text"), "")
        except Exception as e:  # noqa: BLE001
            errors.append(f"CIO 종합 실패: {type(e).__name__}: {e}")
    return desks, cio, errors


def run_brief(model: str | None = None, use_api: bool = True) -> str:
    import analytics

    day = datetime.date.today().isoformat()
    res = analytics.compute("all")
    code_md = analytics.to_markdown("all", res)
    st = res["status"]
    desks, cio, errors = {}, None, []
    if use_api and os.environ.get("ANTHROPIC_API_KEY"):
        desks, cio, errors = _agent_sections(day, model)

    mode = f"AGENT({model or config.MODEL}) + CODE" if desks else "CODE 전용 — API 키 없음(에이전트 해석은 Claude Code market-desk 스킬)"
    head = [f"# 시장 {'브리프' if desks else '지표'} — {day}", "",
            f"> 데이터: 시장 스냅샷 {st['market_generated_at']} · 포지션 {st['positions']} · 생성 {store.now_iso()} · {mode}",
            f"> 프롬프트 버전 {store.prompt_version()} · 본 자료는 의사결정 지원 자료이며 매매 지시가 아님."]
    if st["need_refresh"]:
        head.append("> **데이터 신선도 주의**: " + " / ".join(st["problems"]))
    if errors:
        head.append("> **생성 오류**: " + " / ".join(errors))
    body = []
    if desks:
        team = " ".join(avatar_tag(a, 36) for a in config.AGENTS if a in desks)
        body += ["", team, "", cio or "_(CIO 종합 생성 실패 — 데스크 섹션만 수록)_", "", "## 데스크 섹션", ""]
        body += [f"{avatar_tag(a)}\n\n{desks[a]}\n" for a in config.AGENTS if a in desks]
        body += ["", "---", "", "## 부록 — 시장 분석 지표 (CODE)", "", code_md]
    else:
        body += ["", code_md]

    config.REPORT_DIR.mkdir(exist_ok=True)
    path = config.REPORT_DIR / f"{day}_{'시장브리프' if desks else '시장지표'}.md"
    path.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    store.append_log("BRIEF", {"day": day, "file": path.name, "mode": "AGENT" if desks else "CODE",
                               "signals": len(res["signals"]), "errors": errors})
    return str(path)
