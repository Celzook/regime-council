# -*- coding: utf-8 -*-
"""국면협의회 CLI.

사용법:
  python main.py discuss [--topic "오늘의 논제"]
  python main.py ask "지금 금리 국면 어때?"
  python main.py chat
  python main.py research "미국 하이일드 스프레드 전망" [--agent bond]
  python main.py fetch                      # 시장 스냅샷 갱신 (Refinitiv + VIX yfinance + OAS FRED)
  python main.py brief [--fetch] [--code-only]  # 시장 브리프 → reports/

공통 옵션: --model MODEL_ID (기본 claude-opus-5-5)
"""
import argparse
import sys

# Windows 콘솔(cp949)에서 한글 출력 깨짐 방지
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main():
    p = argparse.ArgumentParser(description="채권·주식·환 국면 협의회")
    p.add_argument("--model", default=None, help="모델 ID 재지정 (기본 config.MODEL)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discuss", help="3개 에이전트 국면 토의 → 회의록")
    d.add_argument("--topic", default=None, help="추가 논제 (선택)")

    a = sub.add_parser("ask", help="단발 질문")
    a.add_argument("question", help="질문")

    sub.add_parser("chat", help="대화형 질의응답")

    r = sub.add_parser("research", help="웹 검색 리서치 → 보고서")
    r.add_argument("topic", help="조사 주제")
    r.add_argument("--agent", default=None, choices=["bond", "equity", "fx"],
                   help="특정 에이전트 관점으로 조사 (기본: 공통 리서처)")

    sub.add_parser("fetch", help="시장 스냅샷 갱신 (data/market.json)")
    sub.add_parser("history", help="과거 시계열 수집·증분 갱신 (data/history/)")
    sub.add_parser("daily", help="일 단위 상시 점검 (한도·손절·스트레스 → 경고·로그·브리프)")
    mpp = sub.add_parser("mp", help="자산별 MP 산출 (산출자+검증자 2패스)")
    mpp.add_argument("asset", choices=["bond", "equity", "fx"])
    ck = sub.add_parser("check", help="투자 포인트 점검 보고서")
    ck.add_argument("trigger", choices=["NEW", "REBAL_6M", "LIMIT_APPROACH", "SHOCK"])
    ck.add_argument("asset", choices=["bond", "equity", "fx"])
    ck.add_argument("--inputs", default="{}", help='JSON 예: \'{"name":"해외 HY","mv_bn":300,"duration":3.5,"currency":"USD","hedge_pct":90}\'')
    mo = sub.add_parser("monthly", help="투자위원회 월간 보고서")
    mo.add_argument("--month", default=None, help="YYYY-MM (기본 이번 달)")
    bk = sub.add_parser("book", help="보유 엑셀(시험 포트) 시가 평가 → data/positions.json")
    bk.add_argument("--limits", action="store_true", help="엑셀 '한도' 시트로 limits.json 도 갱신(체험용)")
    br = sub.add_parser("brief", help="시장 브리프 (CODE 지표 + API 키 있으면 데스크 3곳·CIO 종합)")
    br.add_argument("--code-only", action="store_true", help="API 호출 없이 CODE 지표만")
    br.add_argument("--fetch", action="store_true", help="먼저 시장 스냅샷·과거 시계열 갱신")

    args = p.parse_args()

    if args.cmd == "fetch":
        import fetch_market
        fetch_market.main()
        return
    if args.cmd == "history":
        import fetch_history
        fetch_history.main()
        return
    if args.cmd == "daily":
        import json
        import daily
        d = daily.run_daily(model=args.model)
        print(json.dumps(d["brief"], ensure_ascii=False, indent=1))
        print(f"열린 경고 {len(d['alerts_open'])}건 · 권고 {d['suggested_triggers']} · 보고서 reports/{d['day']}_일일점검.md")
        return
    if args.cmd == "mp":
        import mp
        doc = mp.generate_mp(args.asset, model=args.model)
        print(f"{doc['mp_id']} 생성 — 자체검증 {doc['comparison']['verdict']} ({doc['comparison']['rule']})")
        return
    if args.cmd == "check":
        import json
        import checks
        doc = checks.run_check(args.trigger, args.asset, json.loads(args.inputs), model=args.model)
        print(f"{doc['report_id']} 생성 — 등급 {doc['final_grade']} · 자체검증 {doc['verdict']} · {doc['rule']}")
        return
    if args.cmd == "book":
        import json
        import book
        print(json.dumps(book.sync(with_limits=args.limits), ensure_ascii=False))
        return
    if args.cmd == "brief":
        import brief
        if args.fetch:
            import fetch_history
            import fetch_market
            fetch_market.main()
            fetch_history.main()
        print(f"저장: {brief.run_brief(model=args.model, use_api=not args.code_only)}")
        return
    if args.cmd == "monthly":
        import datetime
        import daily
        month = args.month or datetime.date.today().strftime("%Y-%m")
        daily.monthly_report(month)
        print(f"reports/투자위보고_{month}.md 생성")
        return

    if args.cmd == "discuss":
        from council import run_discussion
        run_discussion(topic=args.topic, model=args.model)
    elif args.cmd == "ask":
        from qa import ask
        ask(args.question, model=args.model)
    elif args.cmd == "chat":
        from qa import chat
        chat(model=args.model)
    elif args.cmd == "research":
        from research import run_research
        run_research(args.topic, agent=args.agent, model=args.model)


if __name__ == "__main__":
    main()
