# -*- coding: utf-8 -*-
"""장 마감 뒤 자동 갱신 — Windows 작업 스케줄러가 평일 16:30 에 부른다(register_refresh_task.ps1).

순서: Workspace 연결 확인 → 과거 시계열 → 시장 스냅샷 → 인포맥스·매크로 → 주식 대시보드 → 정량 → 밸류에이션(12시간 지났을 때) → 밸류에이션 이력(7일 지났을 때)
     → 위탁운용사 펀드(funds.py — 금투협, 20시간 지났을 때) → 급등 매수(spikes.py, 20시간 지났을 때 — 약 70초, timing_daily.csv 도 받는다)
     → 페어트레이딩(pairs.py — 20시간 지났을 때, 종목 가격 증분·DART 지분 캐시)
     → 옵션 풋/콜·미결제약정(options_pc.py — 하루 한 줄씩 쌓음) → 증시자금(kofia_free.py — 금투협 FreeSIS 예탁금·신용융자 증분)
     → 마켓타이밍 시그널(timing.py — 앞 단계 파일만 읽음) → 시험 포트 평가 → 전략 보드 → 백테스트 요약 → 데이터 점검(datahealth.py — 찾은 이상은 실패로 치지 않는다).
한 단계가 실패해도 나머지는 계속한다. 결과는 data/log/refresh_status.json(화면용)과 data/log/refresh_YYYYMMDD.log 에 남는다.
Workspace 가 꺼져 있거나 로그아웃돼 있으면(무인 실행 중 세션 타임아웃 포함) 아무것도 덮어쓰지 않고 Windows 알림을 띄운 뒤 끝낸다.

사용법: python refresh_all.py            # 전체
       python refresh_all.py --quick    # 시장 스냅샷·주식 대시보드만
"""
import datetime
import json
import os
import subprocess
import sys
import time
import traceback

import config

LOG_DIR = config.DATA_DIR / "log"
STATUS = LOG_DIR / "refresh_status.json"


def notify(title: str, body: str) -> None:
    """Windows 알림 풍선 — 실패해도 갱신 결과에는 영향 없음."""
    ps = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
          "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
          f"$n.ShowBalloonTip(10000, '{title}', '{body.replace(chr(39), '')}', 'Warning'); Start-Sleep -Seconds 11; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def _fund_if_stale():
    import fundamentals
    age = fundamentals.age_hours()
    if age is not None and age < fundamentals.STALE_HOURS:
        return f"건너뜀(받은 지 {age:.1f}시간)"
    fundamentals.build()
    return "갱신"


def _valhist_if_stale():
    import valhist
    age = valhist.age_days()
    if age is not None and age < valhist.STALE_DAYS:
        return f"건너뜀(받은 지 {age:.1f}일)"
    valhist.build()
    return "갱신"


def _spikes_if_stale():
    import spikes
    age = spikes.age_hours()
    if age is not None and age < spikes.STALE_HOURS:
        return f"건너뜀(계산한 지 {age:.1f}시간)"
    spikes.build()
    return "갱신"


def _pairs_if_stale():
    import pairs
    age = pairs.age_hours()
    if age is not None and age < pairs.STALE_HOURS:
        return f"건너뜀(계산한 지 {age:.1f}시간)"
    d = pairs.build()
    m = d.get("meta", {})
    return f"채택 {m.get('n_adopted_last', '-')}쌍 · 후보 {sum((m.get('n_candidates') or {}).values())}쌍"


def _funds():
    import funds
    return funds.run()


def _options_pc():
    import options_pc
    return options_pc.run()


def _kofia_free():
    import kofia_free
    return kofia_free.run()


def _timing():
    import timing
    c = timing.build()["composite"]
    return f"합성 {c.get('label') or '-'}"


def _macro():
    import infomax_bridge
    m = infomax_bridge.read().get("meta", {})
    return f"{m.get('mode')} · 인포맥스 {m.get('ok', 0)}/{m.get('total', 0)} · Refinitiv {m.get('refinitiv')}"


def _book():
    import book
    if not book.DEFAULT_BOOK.exists():
        return "건너뜀(보유 엑셀 없음)"
    r = book.sync(quiet=True)
    if r.get("skipped"):
        return f"건너뜀({r.get('error')})"
    return f"손익 {r.get('pnl_bn')}억"


def _health():
    import datahealth
    iss = datahealth.run()["issues"]
    return f"이상 {sum(i['severity'] == 'warn' for i in iss)}건" + (f" · 참고 {sum(i['severity'] == 'info' for i in iss)}건" if iss else "")


def steps(quick: bool):
    import backtest
    import equity
    import fetch_history
    import fetch_market
    import quant
    import strategy
    full = [
        ("과거 시계열", fetch_history.main),
        ("시장 스냅샷", fetch_market.main),
        ("인포맥스·매크로", _macro),
        ("주식 대시보드", lambda: equity.build() and "갱신"),
        ("정량 분석", lambda: quant.run() and "갱신"),
        ("밸류에이션", _fund_if_stale),
        ("밸류에이션 이력", _valhist_if_stale),
        ("위탁운용사 펀드", _funds),                # BM 이 equity_daily·kofia_daily 를 읽으므로 그 갱신 뒤(fund_bm_daily.csv — ACWI·VN Index 는 funds.run 이 계산 전에 직접 갱신)
        ("급등 매수", _spikes_if_stale),           # timing_daily.csv(^VIX3M)를 받으므로 시그널보다 먼저
        ("페어트레이딩", _pairs_if_stale),         # pairs_ret·tv_daily.csv 증분 + DART 지분(캐시) — 20시간 지났을 때
        ("옵션 풋/콜·미결제약정", _options_pc),     # putcall_daily·futures_oi_daily.csv — 시그널보다 먼저
        ("증시자금(금투협)", _kofia_free),          # kofia_free_daily.csv(예탁금·신용융자) — 시그널보다 먼저
        ("마켓타이밍 시그널", _timing),             # 밸류에이션 이력·시장 스냅샷·주식 대시보드·위 세 단계 결과를 읽는다
        ("시험 포트 평가", _book),
        ("전략 보드", lambda: strategy.build() and "갱신"),
        ("백테스트 요약", lambda: backtest.run_all() and "갱신"),
        ("데이터 점검", _health),
    ]
    return [s for s in full if s[0] in ("시장 스냅샷", "주식 대시보드", "데이터 점검")] if quick else full


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now()
    log = open(LOG_DIR / f"refresh_{started:%Y%m%d}.log", "a", encoding="utf-8")

    def say(msg):
        line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
        print(line)
        log.write(line + "\n")
        log.flush()

    import refinitiv_src
    status = {"started_at": started.isoformat(timespec="seconds"), "finished_at": None, "ok": False, "steps": []}
    if not refinitiv_src.available(force=True):
        err = refinitiv_src.status()["error"]
        say(f"중단: {err}")
        status.update(finished_at=datetime.datetime.now().isoformat(timespec="seconds"), error=err)
        STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
        notify("시장 데이터 갱신 실패", "Refinitiv Workspace 가 꺼져 있거나 로그아웃 상태입니다. 로그인 후 다시 실행하세요.")
        log.close()
        return 2

    failed = []
    for name, fn in steps("--quick" in sys.argv):
        t0 = time.time()
        try:
            res = fn()
            msg = res if isinstance(res, str) else "완료"
            status["steps"].append({"name": name, "ok": True, "secs": round(time.time() - t0), "msg": msg})
            say(f"OK   {name} ({time.time() - t0:.0f}초) {msg}")
        except BaseException as e:  # noqa: BLE001 — 한 단계 실패가 나머지를 막지 않게(SystemExit 포함)
            failed.append(name)
            status["steps"].append({"name": name, "ok": False, "secs": round(time.time() - t0), "msg": f"{type(e).__name__}: {e}"})
            say(f"FAIL {name}: {type(e).__name__}: {e}")
            log.write(traceback.format_exc() + "\n")
    status.update(finished_at=datetime.datetime.now().isoformat(timespec="seconds"), ok=not failed, failed=failed)
    tmp = STATUS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATUS)
    say(f"끝 — 실패 {len(failed)}건" + (f": {', '.join(failed)}" if failed else ""))
    if failed:
        notify("시장 데이터 갱신 일부 실패", f"{', '.join(failed)} — data/log 확인")
    log.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
