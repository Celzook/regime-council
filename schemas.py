# -*- coding: utf-8 -*-
"""에이전트 구조화 출력 스키마.

인용 태그 규칙(모든 서술 필드 공통):
- [F] 사실: 제공된 스냅샷·포트·한도 수치 인용 (필드 키·기준일 동반)
- [E] 추정: 에이전트 판단·계산 (산식 동반)
- [A] 가정: MP 가정
"""
from typing import Literal
from pydantic import BaseModel, Field

Tag = Literal["F", "E", "A"]
Asset = Literal["bond", "equity", "fx"]
Grade = Literal["NONE", "WATCH", "WARN", "REDUCE", "HALT"]
Direction = Literal["INCREASE", "REDUCE", "HOLD", "HEDGE_UP", "HEDGE_DOWN"]


class Citation(BaseModel):
    text: str
    tag: Tag
    ref: str | None = Field(default=None, description="인용한 데이터 경로 (예: rates_kr.ktb_3y, positions.CORP_A)")


# ── 국면 토의 (기존) ──────────────────────────────────────────────
class Claim(BaseModel):
    point: str
    used_fields: list[str] = Field(default_factory=list)
    estimate: bool = False


class QuestionTo(BaseModel):
    to: Asset
    question: str


class RegimeView(BaseModel):
    agent: str
    regime_label: str
    confidence: float = Field(ge=0, le=1)
    claims: list[Claim]
    portfolio_implications: list[str]
    invalidation: str
    questions_to_others: list[QuestionTo] = Field(default_factory=list)


class Disagreement(BaseModel):
    with_agent: Asset
    point: str
    argument: str


class CrossView(BaseModel):
    agent: str
    agreements: list[str] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    revised_regime_label: str
    revised_confidence: float = Field(ge=0, le=1)
    closing_remark: str


# ── 모델 포트폴리오(MP) ───────────────────────────────────────────
class MPTarget(BaseModel):
    position_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,40}$",
                             description="포지션 ID (제공된 목록 중 하나, 또는 NEW_<영문설명> 신규 제안; 영숫자·_·- 만)")
    bucket: str = Field(description="포지션 표시명")
    weight_pct: float | None = Field(default=None, description="전체 포트 대비 목표 비중(%). 환 MP 는 null 가능")
    band_pp: float = Field(default=2.0, description="허용 밴드(±%p)")
    duration: float | None = Field(default=None, description="목표 듀레이션(채권)")
    hedge_pct: float | None = Field(default=None, description="목표 환헤지 비율(%) — 해외자산")
    note: str = Field(default="", description="한 줄 근거")


class MPAssumption(BaseModel):
    key: str
    value: float | None = None
    unit: str = ""
    horizon: str = "6M"
    text: str = ""


class ModelPortfolio(BaseModel):
    asset: Asset
    summary: str = Field(description="MP 핵심 한 줄")
    targets: list[MPTarget]
    assumptions: list[MPAssumption] = Field(description="[A] 핵심 가정 (성장·물가·정책·신용·환)")
    rationale: list[Citation]
    expected_contribution_pp: float | None = Field(default=None, description="목표수익률 기여 추정(%p, [E])")
    confidence: float = Field(ge=0, le=1)
    review_triggers: list[str] = Field(description="MP 를 재검토해야 할 조건")
    data_gaps: list[str] = Field(default_factory=list)


# ── 투자 포인트 점검 보고서 ──────────────────────────────────────
class LimitCheckView(BaseModel):
    conclusion: str = Field(description="한도 점검 결론 1~2문장 ([F] 수치 인용)")
    notes: list[Citation] = Field(default_factory=list)


class HistoryCase(BaseModel):
    date: str = Field(description="유사일 (제공된 표의 날짜만)")
    then: str = Field(description="당시 특징·이후 결과 요약 ([F] 표 수치)")
    difference_now: str = Field(description="현재와의 차이 ([E])")


class HistoryCheckView(BaseModel):
    cases: list[HistoryCase]
    overall: str = Field(description="분포 요약 — '유사 국면 n건 중' 형식, 확률·필연 표현 금지")
    caveat: str


class FundamentalAxis(BaseModel):
    axis: Literal["성장", "물가", "정책", "신용"]
    now: str = Field(description="현재 위치 ([F] 제공 수치만, 없으면 '데이터 없음')")
    mp_assumption: str = Field(description="[A] MP 가정")
    consistent: bool | None = Field(description="정합 여부 (데이터 없으면 null)")
    note: str = ""


class FundamentalCheckView(BaseModel):
    axes: list[FundamentalAxis]
    conclusion: str


class Adjustment(BaseModel):
    position_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,40}$")
    bucket: str
    direction: Direction
    weight_now_pct: float | None = None
    weight_proposed_pct: float | None = None
    duration_now: float | None = None
    duration_proposed: float | None = None
    hedge_now_pct: float | None = None
    hedge_proposed_pct: float | None = None
    amount_bn: float | None = Field(default=None, description="금액(억원, 축소는 음수)")
    deadline: str = ""
    rationale: list[Citation]


class CheckReport(BaseModel):
    headline: str = Field(description="결론 1문장")
    limit_check: LimitCheckView
    history_check: HistoryCheckView
    fundamental_check: FundamentalCheckView
    gap_comment: str = Field(description="MP 괴리 해석 ([F] 괴리표 인용)")
    adjustments: list[Adjustment]
    risk_grade: Grade = Field(description="코드 하한 이상으로만 (하향 불가)")
    operator_checks: list[str] = Field(description="운용역이 직접 확인해야 할 항목")
    data_gaps: list[str] = Field(default_factory=list)


class VerifierView(BaseModel):
    """독립 검증자 — 산출자 본문을 보지 않고 동일 입력으로 재산출."""
    risk_grade: Grade
    adjustments: list[Adjustment]
    key_risks: list[str] = Field(description="산출 시 가장 우려한 리스크")
    note: str = ""


# ── 에이전트 룸: 발언·대화 ───────────────────────────────────────
class ProposedAction(BaseModel):
    type: Literal["check", "mp", "discuss", "fetch", "none"] = Field(
        description="check=투자 포인트 점검 실행, mp=MP 산출, discuss=3자 토의, fetch=시장 갱신, none=행동 없음")
    label: str = Field(description="버튼 문구 (예: '손실한도 접근 점검 실행')")
    trigger: Literal["NEW", "REBAL_6M", "LIMIT_APPROACH", "SHOCK"] | None = Field(default=None, description="check 일 때 트리거")
    note: str = Field(default="", description="왜 이 행동이 필요한지 한 줄")


class AgentVoice(BaseModel):
    """에이전트의 '오늘의 보고' — 1인칭, 담당 포트 중심."""
    headline: str = Field(description="첫 마디 한 문장 (1인칭, 가장 중요한 것)")
    observations: list[str] = Field(description="2~4개. 담당 포트·시장에서 본 것, [F] 수치 인용")
    concerns: list[str] = Field(default_factory=list, description="0~3개. 걱정되는 것과 이유")
    proposed_actions: list[ProposedAction] = Field(default_factory=list, description="0~3개. 운용역에게 제안하는 행동")
    question_for_user: str | None = Field(default=None, description="운용역에게 묻고 싶은 것 (없으면 null)")


class ChatReply(BaseModel):
    """에이전트 대화 응답."""
    text: str = Field(description="답변 본문 (1인칭, 간결, 수치는 [F] 인용)")
    proposed_actions: list[ProposedAction] = Field(default_factory=list, description="필요할 때만. 0~2개")


# ── 일일 브리프 ──────────────────────────────────────────────────
class DailyBrief(BaseModel):
    headline: str = Field(description="1문장: 정상 / 경고 n건 / 축소 n건")
    interpretation: list[str] = Field(description="신호·스트레스·괴리 해석 3줄 이내, [F] 인용")
    todo: list[str] = Field(description="오늘 할 일")
