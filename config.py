# -*- coding: utf-8 -*-
"""국면협의회(regime-council) 설정."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROMPT_DIR = BASE_DIR / "prompts"
DATA_DIR = BASE_DIR / "data"
REPORT_DIR = BASE_DIR / "reports"

MODEL = "claude-opus-5-5"

# 모드별 reasoning effort (opus-5-5 의 기본값은 medium 이므로 명시적으로 지정)
EFFORT = {
    "discuss": "high",      # 국면 토의 라운드
    "synthesis": "high",    # 회의록 종합
    "ask": "medium",        # 질의응답
    "research": "high",     # 웹 리서치
}

MAX_TOKENS = {
    "opinion": 8000,        # 구조화 의견 (JSON) — thinking 토큰 포함
    "answer": 8000,         # 질의응답 — thinking 토큰 포함
    "synthesis": 16000,     # 회의록
    "research": 16000,      # 리서치 보고서
}

# 전략 가정치 — analytics.py 가 [A] 로 표기해 비교 기준으로 쓴다. 문서가 바뀌면 여기만 고친다.
# 가상 설정(예시) — 실제 조직·계획과 무관. 운용 구조는 data/portfolio.json, 한도는 data/limits.json.
STRATEGY = {
    "hedge_cost_assumption_pct": 1.5,  # 연 환헤지 비용 가정 (%p)
    "fx_overlay_limit_bn": 1000,       # 환 오버레이 한도 (명목, 억원) — 가상 설정
}

# 인포맥스(유료 단말) 데이터를 외부 AI(Anthropic)로 보내도 되는지 — 라이선스·준법 확인 전에는 False.
# False 면 인포맥스 값은 화면·코드 계산에만 쓰고, 에이전트 컨텍스트·대화 '화면 공유'에서는 뺀다.
SHARE_VENDOR_DATA = False
# Refinitiv(LSEG) 값을 외부 AI(Anthropic)로 보내도 되는지. 값은 로컬 전용(저장소·공유본 제외)이고,
# 준법 확인 결과 막아야 하면 False 로 — 에이전트 컨텍스트에서 Refinitiv 원천 수치를 뺀다.
SHARE_REFINITIV_DATA = True
# Refinitiv App Key 파일 후보(환경변수 EIKON_APP_KEY 가 우선). Windows 파이썬에서 WSL 홈을 읽는 경로 포함.
REFINITIV_APPKEY_FILES = [
    "~/.claude/reference/refinitiv-workspace/.appkey",
    r"\\wsl.localhost\Ubuntu\home\infomax\.claude\reference\refinitiv-workspace\.appkey",
]
# 매크로 화면을 보고 있을 때 인포맥스 값을 다시 받는 주기(분) — 한 번에 숨김 엑셀이 약 10초 떴다 닫힌다.
INFOMAX_REFRESH_MIN = 5

# 내부 키(bond/equity/fx)는 데이터·파일명에 쓰이므로 그대로 두고, 사람이 보는 이름만 바꾼다.
AGENTS = ["bond", "equity", "fx"]
AGENT_NAMES = {"bond": "BOND", "equity": "STOCK", "fx": "FX"}

# 에이전트 룸에서 보이는 '사람' 설정 — 이름·데스크·그림·한 줄 역할·말투. 이름은 자유롭게 바꿔도 된다.
AGENT_PERSONAS = {
    "bond": {"name": "BOND", "desk": "채권 데스크", "avatar": "static/avatars/bond.svg",
             "tagline": "Active 채권 3,000억 · 국내외 금리·크레딧 담당",
             "voice": "숫자부터 말하고 결론은 짧게. 캐리·롤다운·콜 일정에 민감한 15년차 채권 운용역 톤."},
    "equity": {"name": "STOCK", "desk": "주식 데스크", "avatar": "static/avatars/stock.svg",
               "tagline": "Active 주식 2,000억 · 국내외 주식 담당",
               "voice": "포지션별 목표·손절과 페어 순베타를 먼저 챙기는 톤. 이익·밸류에이션·리스크선호를 함께 본다."},
    "fx": {"name": "FX", "desk": "환 데스크", "avatar": "static/avatars/fx.svg",
           "tagline": "환 오버레이(한도 1,000억) · 해외자산 헤지 담당",
           "voice": "헤지비용과 환오픈 손익을 먼저 보고하는 신중한 톤. 환 판단의 불확실성을 솔직히 말한다."},
}

# 리서치 모드 웹 검색 최대 횟수 (검색 1,000회당 $10 별도 과금)
WEB_SEARCH_MAX_USES = 8

# ask 모드 키워드 라우팅 (모두 불일치 시 3개 에이전트 전원에게 질의)
# 영문·숫자 키워드는 route() 에서 단어 경계로 매칭된다 (per ⊄ super).
# 한 글자 한글 키워드 금지 ('환' 은 '환경·전환·상환' 에 걸림).
ROUTE_KEYWORDS = {
    "bond": ["bond", "채권", "금리", "국고", "크레딧", "스프레드", "듀레이션", "회사채",
             "자본성", "신종자본", "후순위", "콜", "커브", "롤다운", "국채", "기준금리",
             "하이일드", "clo", "사모대출"],
    "equity": ["stock", "주식", "증시", "코스피", "kospi", "s&p", "나스닥", "밸류에이션",
               "per", "pbr", "배당", "이익", "운용사", "gtaa"],
    "fx": ["fx", "환율", "환헤지", "환오픈", "환노출", "환포지션", "외환", "달러", "원화",
           "usd", "krw", "헤지", "스왑포인트", "dxy", "오버레이"],
}
