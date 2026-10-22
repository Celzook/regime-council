# -*- coding: utf-8 -*-
"""국면 토의 오케스트레이션.

라운드 1 (블라인드): 세 에이전트가 서로의 의견을 보지 않고 국면 판단 제출 — 앵커링·동조화 방지.
라운드 2 (교차):    라운드 1 전체 공개 후 반박·동의·질문 답변·판단 수정.
종합:               서기 호출 1회로 회의록 생성.

흐름 제어는 전부 이 코드가 한다(모델이 라운드를 늘리거나 줄일 수 없음).
"""
import datetime
import json
from concurrent.futures import ThreadPoolExecutor

import anthropic

import config
from agent import Agent, build_shared_context, load_prompt
from schemas import CrossView, RegimeView

R1_INSTRUCTION = """\
[라운드 1 — 블라인드 국면 판단]
다른 에이전트의 의견은 아직 공개되지 않았다. 당신의 전담 영역 관점에서:
1. 현재 시장 국면을 판단하라 (regime_label, confidence).
2. 근거 주장(claims)마다 인용한 스냅샷 필드 키를 used_fields 에 적어라.
3. 우리 포트폴리오 관점의 시사점을 적어라.
4. 이 판단이 틀렸음을 가장 먼저 보여줄 지표·조건(invalidation)을 적어라.
5. 다른 에이전트에게 확인하고 싶은 질문을 1건 이상 적어라.
{topic}"""

R2_INSTRUCTION = """\
[라운드 2 — 교차 토의]
세 에이전트의 라운드 1 의견 전체가 아래에 공개됐다 (당신 것 포함).

{others}

1. 동의하는 주장은 agreements 에 (같은 말 반복 금지, 요지만).
2. 반박할 주장은 disagreements 에 — 근거와 함께. 최소 1건을 찾아라.
   정말 반박할 것이 없으면, '세 판단이 동시에 틀리는 가장 그럴듯한 시나리오'를 적어라.
3. 당신에게 온 질문(questions_to_others 중 to={name})에 answers 로 답하라.
4. 토의를 반영해 국면 판단을 수정하거나 유지하라 (revised_regime_label, revised_confidence).
   바꿨다면 무엇 때문인지 closing_remark 에 포함하라."""


def _dump(view) -> str:
    return json.dumps(view.model_dump(), ensure_ascii=False, indent=1)


def run_discussion(topic: str | None = None, model: str | None = None) -> str:
    client = anthropic.Anthropic()
    shared = build_shared_context()
    agents = {n: Agent(n, shared, client, model=model) for n in config.AGENTS}

    topic_line = f"\n[오늘의 추가 논제] {topic}" if topic else ""
    print("라운드 1 — 블라인드 국면 판단 제출 중 (병렬 3건)...")
    with ThreadPoolExecutor(max_workers=3) as pool:
        r1_futures = {
            n: pool.submit(
                ag.opinion, R1_INSTRUCTION.format(topic=topic_line), RegimeView
            )
            for n, ag in agents.items()
        }
    r1 = {n: f.result() for n, f in r1_futures.items()}
    for n, v in r1.items():
        label = v.regime_label if v else "(의견 없음)"
        conf = f" (확신도 {v.confidence:.2f})" if v else ""
        print(f"  [{config.AGENT_NAMES[n]}] {label}{conf}")

    all_r1 = "\n\n".join(
        f"### {config.AGENT_NAMES[n]} 에이전트 (agent={n})\n{_dump(v)}"
        for n, v in r1.items() if v is not None
    )

    print("라운드 2 — 교차 토의 중 (병렬 3건)...")
    with ThreadPoolExecutor(max_workers=3) as pool:
        r2_futures = {
            n: pool.submit(
                ag.opinion,
                R2_INSTRUCTION.format(others=all_r1, name=n),
                CrossView,
            )
            for n, ag in agents.items() if r1[n] is not None
        }
    r2 = {n: f.result() for n, f in r2_futures.items()}
    for n, v in r2.items():
        if v:
            print(f"  [{config.AGENT_NAMES[n]}] 이견 {len(v.disagreements)}건, "
                  f"확신도 {r1[n].confidence:.2f}→{v.revised_confidence:.2f}")

    print("회의록 작성 중...")
    today = datetime.date.today().isoformat()
    transcript = (
        f"[토의 일자] {today}\n[논제] {topic or '정기 국면 점검'}\n\n"
        f"## 라운드 1 (블라인드 제출)\n{all_r1}\n\n## 라운드 2 (교차 토의)\n"
        + "\n\n".join(
            f"### {config.AGENT_NAMES[n]} 에이전트\n{_dump(v)}"
            for n, v in r2.items() if v is not None
        )
    )
    with client.messages.stream(
        model=model or config.MODEL,
        max_tokens=config.MAX_TOKENS["synthesis"],
        output_config={"effort": config.EFFORT["synthesis"]},
        system=[
            {"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": load_prompt("synthesis")},
        ],
        messages=[{"role": "user", "content": transcript}],
    ) as stream:
        minutes = stream.get_final_message()
    minutes_text = next((b.text for b in minutes.content if b.type == "text"), "")

    config.REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    path = config.REPORT_DIR / f"{stamp}_국면토의.md"
    appendix = (
        "\n\n---\n\n<details><summary>부록 — 라운드별 원문 (재현용)</summary>\n\n"
        f"```\n{transcript}\n```\n</details>\n"
    )
    path.write_text(minutes_text + appendix, encoding="utf-8")
    # 에이전트 룸에서 대화 형식으로 보여주기 위한 구조화 기록
    (config.REPORT_DIR / f"{stamp}_국면토의.json").write_text(json.dumps({
        "date": today, "topic": topic, "minutes": path.name,
        "r1": {n: v.model_dump() for n, v in r1.items() if v},
        "r2": {n: v.model_dump() for n, v in r2.items() if v},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n회의록 저장: {path}")
    return str(path)
