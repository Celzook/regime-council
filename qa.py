# -*- coding: utf-8 -*-
"""질의응답 모드.

- route():           키워드 기반 라우팅 (API 호출 없음, 결정적). 매칭 없으면 전원.
- answer_question(): 핵심 로직 — CLI 와 웹 대시보드가 공유.
- ask() / chat():    CLI 래퍼.
"""
import datetime
import re
from concurrent.futures import ThreadPoolExecutor

import anthropic

import config
from agent import Agent, build_shared_context

ASK_PREFIX = (
    "[질의응답] 아래 질문에 당신의 전담 영역 관점에서 답하라. "
    "간결하게, 수치는 스냅샷 인용 규칙대로. 질문이 당신 영역 밖이면 한 줄로 "
    "해당 영역 에이전트에게 넘긴다고 답하라.\n\n질문: "
)


def _kw_match(q: str, w: str) -> bool:
    if re.fullmatch(r"[a-z0-9&]+", w):
        # 영문·숫자 키워드는 단어 경계로 (per 가 super 에 걸리지 않게)
        return re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", q) is not None
    return w in q


def route(question: str) -> list[str]:
    q = question.lower()
    hit = [
        name
        for name, words in config.ROUTE_KEYWORDS.items()
        if any(_kw_match(q, w) for w in words)
    ]
    return hit or list(config.AGENTS)


def new_state(model: str | None = None) -> dict:
    """클라이언트·공유 컨텍스트·에이전트·대화 히스토리를 담는 세션 상태."""
    client = anthropic.Anthropic()
    shared = build_shared_context()
    return {
        "client": client,
        "shared": shared,
        "agents": {n: Agent(n, shared, client, model=model) for n in config.AGENTS},
        "history": [],
        "model": model,
    }


def answer_question(question: str, state: dict,
                    targets: list[str] | None = None) -> dict:
    """질문 1건 처리. targets 미지정 시 키워드 라우팅.

    반환: {"targets": [...], "answers": {agent: text}, "summary": text|None}
    """
    targets = targets or route(question)
    targets = [t for t in targets if t in config.AGENTS]
    agents = state["agents"]
    history = state["history"]

    def _one(n):
        return n, agents[n].answer(ASK_PREFIX + question, history=history)

    with ThreadPoolExecutor(max_workers=3) as pool:
        answers = dict(pool.map(_one, targets))

    summary = None
    if len(answers) > 1:
        joined = "\n\n".join(
            f"[{config.AGENT_NAMES[n]}]\n{a}" for n, a in answers.items()
        )
        resp = state["client"].messages.create(
            model=state.get("model") or config.MODEL,
            max_tokens=2000,
            output_config={"effort": "medium"},
            system="세 운용 에이전트의 답변을 3~6줄로 종합하라. 일치점과 이견을 구분하고, 새 수치를 만들지 마라.",
            messages=[{"role": "user", "content": f"질문: {question}\n\n{joined}"}],
        )
        summary = next((b.text for b in resp.content if b.type == "text"), "")

    _log(question, answers, summary)

    # 빈 content 는 API 가 400 으로 거절하므로(마지막이 아닌 메시지) 히스토리에 넣지 않는다
    combined = (summary or "\n".join(answers.values())).strip()
    if combined:
        history.extend(
            [
                {"role": "user", "content": question},
                {"role": "assistant", "content": combined},
            ]
        )
    state["history"] = history[-8:]  # 최근 4문답만 유지

    return {"targets": targets, "answers": answers, "summary": summary}


def _log(question: str, answers: dict[str, str], summary: str | None):
    config.REPORT_DIR.mkdir(exist_ok=True)
    path = config.REPORT_DIR / "qa_log.md"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## {stamp} — {question}\n"]
    for n, a in answers.items():
        lines.append(f"### {config.AGENT_NAMES[n]}\n{a}\n")
    if summary:
        lines.append(f"### 종합\n{summary}\n")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def ask(question: str, model: str | None = None, _state: dict | None = None):
    state = _state or new_state(model)
    result = answer_question(question, state)
    print(f"→ 응답 에이전트: "
          f"{', '.join(config.AGENT_NAMES[n] for n in result['targets'])}\n")
    for n, a in result["answers"].items():
        print(f"◆ {config.AGENT_NAMES[n]}\n{a}\n")
    if result["summary"]:
        print(f"◆ 종합\n{result['summary']}\n")


def chat(model: str | None = None):
    print("국면 협의회 대화 모드. 빈 줄 또는 'q' 입력 시 종료.\n")
    state = new_state(model)
    while True:
        try:
            q = input("질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in ("q", "quit", "exit"):
            break
        ask(q, model=model, _state=state)
