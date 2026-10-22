# -*- coding: utf-8 -*-
"""리서치 모드 — 웹 검색 서버 도구 사용.

web_search 는 Anthropic 서버에서 실행된다 (클라이언트 구현 없음).
stop_reason == "pause_turn" 이면 응답 내용을 그대로 이어붙여 재요청한다.
검색 비용: 1,000회당 $10 (토큰 비용 별도) — max_uses 로 상한을 건다.
"""
import datetime
import re

import anthropic

import config
from agent import build_shared_context, load_prompt


def run_research(topic: str, agent: str | None = None,
                 model: str | None = None) -> str:
    client = anthropic.Anthropic()
    system = [
        {
            "type": "text",
            "text": build_shared_context(),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": load_prompt("research")},
    ]
    if agent in config.AGENTS:
        system.append({"type": "text", "text": load_prompt(agent)})

    today = datetime.date.today().isoformat()
    messages = [
        {
            "role": "user",
            "content": f"오늘은 {today}다. 다음 주제를 조사해 보고서를 작성하라: {topic}",
        }
    ]
    tools = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": config.WEB_SEARCH_MAX_USES,
        }
    ]

    print(f"리서치 시작: {topic} (웹 검색 최대 {config.WEB_SEARCH_MAX_USES}회)")
    while True:
        with client.messages.stream(
            model=model or config.MODEL,
            max_tokens=config.MAX_TOKENS["research"],
            output_config={"effort": config.EFFORT["research"]},
            system=system,
            tools=tools,
            messages=messages,
        ) as stream:
            resp = stream.get_final_message()
        searches = sum(1 for b in resp.content if b.type == "server_tool_use")
        if searches:
            print(f"  ... 검색 {searches}건 수행")
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break

    if resp.stop_reason == "refusal":
        raise SystemExit("리서치 응답이 거절되었습니다 (stop_reason=refusal).")

    report = "\n\n".join(b.text for b in resp.content if b.type == "text")

    config.REPORT_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^\w가-힣]+", "_", topic)[:30].strip("_")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    path = config.REPORT_DIR / f"{stamp}_리서치_{slug}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\n보고서 저장: {path}")
    return str(path)
