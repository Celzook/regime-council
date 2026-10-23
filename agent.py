# -*- coding: utf-8 -*-
"""에이전트 공통 클래스.

시스템 프롬프트 구성 (캐시 효율을 위해 공통 블록을 앞에 둔다):
  [0] 공통 컨텍스트 + 시장/포트폴리오 스냅샷  ← cache_control (에이전트 간 공유)
  [1] 에이전트 역할 프롬프트
"""
import json

import anthropic

import config


def load_prompt(name: str) -> str:
    return (config.PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


# 파일별 마지막 읽기 오류 (웹 대시보드가 사용자에게 원인을 보여주는 용도)
LOAD_ERRORS: dict[str, str] = {}


def load_json(path) -> dict | None:
    """깨진 JSON·인코딩 오류 시 None 반환 — 파일 하나가 전체를 죽이지 않게."""
    if not path.exists():
        LOAD_ERRORS.pop(path.name, None)  # 삭제된 파일의 과거 오류는 지운다
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        LOAD_ERRORS.pop(path.name, None)
        return data
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        LOAD_ERRORS[path.name] = f"{type(e).__name__}: {e}"
        print(f"경고: {path.name} 읽기 실패 — {e} (UTF-8 저장·JSON 문법을 확인하세요)")
        return None


def build_shared_context() -> str:
    """공통 프롬프트 + 데이터 스냅샷을 하나의 텍스트 블록으로."""
    parts = [load_prompt("common")]

    market = load_json(config.DATA_DIR / "market.json") or load_json(
        config.DATA_DIR / "market.sample.json"
    )
    if market is None:
        raise RuntimeError(
            "data/market.json 을 읽을 수 없습니다. fetch_market.py 를 실행하거나 "
            "market.sample.json 을 복사해 수정하세요. (UTF-8·JSON 문법 확인)"
        )
    # Refinitiv 원천 수치를 외부 AI 로 보내지 않기로 하면(config.SHARE_REFINITIV_DATA=False) 시장 스냅샷과
    # 그 값에서 계산한 절(시장 지표·정량·주식·밸류에이션)을 통째로 뺀다 — 포트·한도·전략 보드만 남는다.
    share_mkt = getattr(config, "SHARE_REFINITIV_DATA", True)
    if share_mkt:
        parts.append(
            "\n# 시장 스냅샷 (아래 수치만 인용 가능)\n```json\n"
            + json.dumps(market, ensure_ascii=False, indent=1)
            + "\n```"
        )
    else:
        parts.append("\n# 시장 스냅샷\n(Refinitiv 데이터 외부 전송 제한 — config.SHARE_REFINITIV_DATA=False. "
                     "시장 수치는 인용하지 말고 '데이터 없음'으로 처리)")

    portfolio = load_json(config.DATA_DIR / "portfolio.json") or load_json(
        config.DATA_DIR / "portfolio.sample.json"
    )
    if portfolio is not None:
        parts.append(
            "\n# 포트폴리오 스냅샷 (집계 수준)\n```json\n"
            + json.dumps(portfolio, ensure_ascii=False, indent=1)
            + "\n```"
        )
    else:
        parts.append("\n# 포트폴리오 스냅샷\n(미제공 — 시장 국면만 논의)")

    if not share_mkt:
        parts.append(_strategy_md())
        return "\n".join(parts)
    try:
        import analytics  # 지연 import — analytics 가 이 모듈의 load_json 을 쓴다
        parts.append("\n" + analytics.to_markdown("all"))
    except Exception as e:  # noqa: BLE001 — 지표 계산 실패가 에이전트 전체를 막지 않게
        parts.append(f"\n## 시장 분석 지표\n(계산 실패: {type(e).__name__}: {e} — 이 절의 수치는 '데이터 없음'으로 처리)")
    try:
        import quant  # Refinitiv 가격 기반 — SHARE_REFINITIV_DATA 가 True 일 때만 여기까지 온다
        d = quant.load(max_age_hours=36)
        if d:
            parts.append("\n" + quant.to_markdown(d))
    except Exception as e:  # noqa: BLE001
        parts.append(f"\n## 정량 분석\n(불러오기 실패: {type(e).__name__}: {e})")
    try:
        import equity  # 주식 대시보드(Refinitiv 가격·유니버스 전략 목록)
        d = equity.load(max_age_hours=36)
        if d:
            parts.append("\n" + equity.to_markdown(d))
    except Exception as e:  # noqa: BLE001
        parts.append(f"\n## 주식 시장 대시보드\n(불러오기 실패: {type(e).__name__}: {e})")
    try:
        import fundamentals  # 유니버스 밸류에이션(Refinitiv)
        d = fundamentals.load()
        if d:
            parts.append("\n" + fundamentals.to_markdown(d))
    except Exception as e:  # noqa: BLE001
        parts.append(f"\n## 주식 유니버스 밸류에이션\n(불러오기 실패: {type(e).__name__}: {e})")
    parts.append(_strategy_md())
    return "\n".join(parts)


def _strategy_md() -> str:
    try:
        import strategy  # 규칙 기반 '지금 할 일' — 에이전트는 이 판단을 설명·반론만 한다
        d = strategy.load(max_age_hours=36)
        return "\n" + strategy.to_markdown(d) if d else ""
    except Exception as e:  # noqa: BLE001
        return f"\n## 전략 보드\n(불러오기 실패: {type(e).__name__}: {e})"


class Agent:
    def __init__(self, name: str, shared_context: str, client: anthropic.Anthropic,
                 model: str | None = None, extra_system: str | None = None):
        self.name = name
        self.korean = config.AGENT_NAMES[name]
        self.client = client
        self.model = model or config.MODEL
        self.system = [
            {
                "type": "text",
                "text": shared_context,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": load_prompt(name)},
        ]
        if extra_system:
            # 오늘의 담당 포트 상태 등 — 캐시 블록 뒤에 둬서 공통 프리픽스 캐시를 깨지 않는다
            self.system.append({"type": "text", "text": extra_system})
        self.messages: list[dict] = []  # 에이전트별 토의 히스토리

    def opinion(self, instruction: str, output_format, mode: str = "discuss"):
        """구조화 의견 요청 (Pydantic 검증). 실패/거절/잘림 시 None."""
        self.messages.append({"role": "user", "content": instruction})
        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=config.MAX_TOKENS["opinion"],
                output_config={"effort": config.EFFORT[mode]},
                system=self.system,
                messages=self.messages,
                output_format=output_format,
            )
            if resp.stop_reason == "refusal":
                print(f"  [{self.korean}] 응답 거절(stop_reason=refusal) — 의견 없음 처리")
                raise ValueError("refusal")
            if resp.stop_reason == "max_tokens":
                print(f"  [{self.korean}] 응답이 max_tokens 에 잘림 — 의견 없음 처리")
                raise ValueError("max_tokens")
            view = resp.parsed_output
            if view is None:
                raise ValueError("parsed_output 없음")
        except Exception as e:  # noqa: BLE001 — 계약: 실패 시 None
            if not isinstance(e, ValueError):
                print(f"  [{self.korean}] 의견 생성 실패: {e}")
            self.messages.pop()
            return None
        self.messages.append(
            {"role": "assistant", "content": view.model_dump_json()}
        )
        return view

    def answer(self, question: str, history: list[dict] | None = None,
               mode: str = "ask") -> str:
        """자유 서술 답변 (질의응답용). history 는 [{'role','content'}] 추가 문맥."""
        msgs = (history or []) + [{"role": "user", "content": question}]
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=config.MAX_TOKENS["answer"],
            output_config={"effort": config.EFFORT[mode]},
            system=self.system,
            messages=msgs,
        )
        if resp.stop_reason == "refusal":
            return "(응답 거절 — 의견 없음)"
        text = next((b.text for b in resp.content if b.type == "text"), "")
        if resp.stop_reason == "max_tokens":
            text += "\n\n(주의: 응답이 길이 제한에 잘렸습니다)"
        return text
