# -*- coding: utf-8 -*-
"""AI 운용관리 대시보드 (로컬 전용).  실행: python web.py → http://127.0.0.1:5101

화면: 홈(한도 게이지·신호·괴리·할 일) / 점검 보고서 / MP·괴리 / 한도·로그 / 투자위 보고서 / 참고(매크로·채팅·토의)
- 한도·소진율·스트레스·괴리·경고는 코드(CODE) 계산 — API 키 없이 동작
- MP 산출·점검 보고서 해석·브리프·채팅·토의는 Claude(AGENT) — ANTHROPIC_API_KEY 필요
- 127.0.0.1 바인딩, Origin/Host 검사(CSRF). REGIME_LAN=1 이면 사내 공유(허용 대역·접속 키·읽기 전용)
"""
import datetime
import os
import re
import sys
import threading
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider

import checks
import config
import daily
import limits
import mp as mp_mod
import qa
import store
from agent import LOAD_ERRORS, load_json

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

class CleanJSON(DefaultJSONProvider):
    """NaN/inf 를 null 로 — 브라우저 JSON.parse 가 깨지지 않게."""
    sort_keys = False

    def dumps(self, obj, **kwargs):
        return super().dumps(store._clean(obj), **kwargs)


app = Flask(__name__, static_folder="static")
app.json = CleanJSON(app)


@app.errorhandler(Exception)
def _api_error(e):
    """API 경로의 예상 못한 예외는 JSON 으로 — 화면이 '서버 응답 없음' 으로 오해하지 않게."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    if request.path.startswith("/api/"):
        msg = f"{type(e).__name__}: {e}" if _is_local() else "서버 오류 — 운영 PC 로그를 확인하세요"
        return jsonify({"error": msg}), 500
    return "서버 오류", 500


ALLOWED_HOSTS = {"127.0.0.1:5101", "localhost:5101"}
ALLOWED_ORIGINS = {"http://127.0.0.1:5101", "http://localhost:5101"}

CHAT = {"state": None}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
FETCH_LOCK = threading.Lock()


LAN = {"key": None, "ip": None}


def _lan_setup() -> None:
    """사내 공유를 켤 때 — 접속 키를 준비하고, 이 PC 의 사내 주소를 허용 호스트에 넣는다."""
    import secrets
    import socket
    p = config.LAN_KEY_FILE
    if not p.exists():
        p.write_text(secrets.token_urlsafe(12), encoding="utf-8")
    key = p.read_text(encoding="utf-8").strip()
    if len(key) < 16:
        raise SystemExit(f"{p} 가 비었거나 너무 짧습니다 — 파일을 지우고 다시 켜면 새로 만듭니다.")
    LAN["key"] = key
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        LAN["ip"] = s.getsockname()[0]
    except OSError:
        LAN["ip"] = socket.gethostbyname(socket.gethostname())
    finally:
        s.close()
    ALLOWED_HOSTS.add(f"{LAN['ip']}:5101")
    ALLOWED_ORIGINS.add(f"http://{LAN['ip']}:5101")


def _key_ok() -> bool:
    """접속 키 확인 — 키가 준비되지 않았으면 무조건 거부(fail closed), 비교는 상수 시간."""
    import hmac
    k = LAN["key"]
    if not k:
        return False
    return any(c and hmac.compare_digest(c.encode(), k.encode())
               for c in (request.args.get("key"), request.cookies.get("rc_key")))


REMOTE_BT = threading.BoundedSemaphore(2)   # 원격 백테스트 동시 실행 상한 — 운영 화면이 느려지지 않게


def _is_local() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


def _lan_allowed(addr: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(n, strict=False) for n in config.LAN_ALLOW)


def _lan_get_ok(path: str) -> bool:
    """사내 공유 보기의 GET 허용 목록(config.LAN_GET_ALLOW) — '*' 로 끝나는 항목은 앞부분 일치, 나머지는 같은 주소만."""
    return any(path.startswith(x[:-1]) if x.endswith("*") else path == x for x in config.LAN_GET_ALLOW)


@app.before_request
def _csrf_guard():
    if request.host not in ALLOWED_HOSTS:
        return jsonify({"error": "허용되지 않은 호스트"}), 403
    if not _is_local():
        # 사내 공유 보기: 허용 대역 · 접속 키 · 읽기 전용(허용된 계산 하나만) · 조회는 화면이 쓰는 주소만
        if not config.LAN_SHARE or not _lan_allowed(request.remote_addr):
            return jsonify({"error": "허용되지 않은 접속"}), 403
        if not _key_ok():
            return "접속 키가 필요합니다 — 공유받은 링크(…/?key=…)로 여세요.", 403
        if any(request.path.startswith(x) for x in config.LAN_HIDE):
            return jsonify({"error": "사내 공유 보기에서는 볼 수 없는 화면입니다."}), 403
        if request.method == "POST" and request.path not in config.LAN_POST_ALLOW:
            return jsonify({"error": "사내 공유 보기는 읽기 전용입니다 — 갱신·점검·AI 기능은 운영 PC 에서만 됩니다."}), 403
        if request.method != "POST" and not _lan_get_ok(request.path):
            return jsonify({"error": "사내 공유 보기에서는 볼 수 없는 화면입니다."}), 403
        if request.method == "GET" and "key" in request.args:
            # 키를 쿠키로 옮기고 주소창·방문 기록·프록시 로그에서 지운다
            from flask import redirect
            resp = redirect(request.path, 302)
            resp.set_cookie("rc_key", LAN["key"], max_age=7 * 86400, httponly=True, samesite="Strict")
            return resp
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return jsonify({"error": "허용되지 않은 출처"}), 403
    return None


@app.after_request
def _security_headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"                     # 다른 사이트가 이 화면을 프레임에 넣지 못하게
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def api_key_set() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _err(msg, code=400):
    return jsonify({"error": msg}), code


# ── 백그라운드 작업 ────────────────────────────────────────────
def start_job(kind: str, fn, *args, **kwargs) -> str:
    job_id = f"{kind}-{datetime.datetime.now().strftime('%H%M%S%f')[:9]}"
    with JOBS_LOCK:
        running = [j for j in JOBS.values() if j["kind"] == kind and j["status"] == "running"]
        if running and kind in ("discuss", "daily", "equity", "fund"):
            raise RuntimeError(f"{kind} 작업이 이미 진행 중입니다.")
        JOBS[job_id] = {"job_id": job_id, "kind": kind, "status": "running", "started_at": store.now_iso(),
                        "result": None, "error": None}

    def _run():
        try:
            res = fn(*args, **kwargs)
            JOBS[job_id].update({"status": "done", "result": res, "finished_at": store.now_iso()})
        except BaseException as e:  # noqa: BLE001
            traceback.print_exc()
            JOBS[job_id].update({"status": "error", "error": str(e) or type(e).__name__, "finished_at": store.now_iso()})

    threading.Thread(target=_run, daemon=True).start()
    return job_id


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    j = JOBS.get(job_id)
    if not j:
        return _err("작업 없음", 404)
    return jsonify(j)


@app.get("/api/jobs")
def api_jobs():
    return jsonify(sorted(JOBS.values(), key=lambda j: j["started_at"], reverse=True)[:20])


# ── 정적 ──────────────────────────────────────────────────────
@app.get("/")
def index():
    return send_from_directory("static", "index.html")


# ── 홈/개요 ───────────────────────────────────────────────────
_QUOTED_PATH = re.compile(r"'((?:[A-Za-z]:|\\\\|/)[^'\n]*)'")   # OSError 문장 속 '경로' (공백 포함)


def _warnings(problems=()) -> list:
    """data_warnings — 원격(사내 공유) 응답에는 OSError 문장 속 이 PC 폴더 경로 대신 파일 이름만."""
    w = [f"{n}: {e}" for n, e in LOAD_ERRORS.items()] + list(problems)
    if _is_local():
        return w
    return [_QUOTED_PATH.sub(lambda m: "'" + re.split(r"[\\/]+", m.group(1))[-1] + "'", x) for x in w]


def _hide_paths(latest_daily):
    """원격(사내 공유) 응답에는 일일 점검 기록의 보유·한도 파일 경로 대신 파일 이름만 — 원본(캐시)은 건드리지 않는다."""
    bk = (latest_daily or {}).get("book") if isinstance(latest_daily, dict) else None
    if _is_local() or not isinstance(bk, dict):
        return latest_daily
    from pathlib import PureWindowsPath
    bk = {k: PureWindowsPath(str(v)).name if k in ("positions_path", "limits_path") and v else v for k, v in bk.items()}
    return {**latest_daily, "book": bk}


@app.get("/api/overview")
def api_overview():
    lim = limits.load_limits()
    problems = []
    try:
        ev = limits.evaluate(limits=lim)
    except Exception as e:  # noqa: BLE001 — 데이터 파일 문제로 전체 화면이 죽지 않게 부분 응답
        traceback.print_exc()
        problems.append(f"한도 계산 실패: {type(e).__name__}: {e} — data/positions.json·limits.json 확인")
        ev = None
    try:
        alerts = [a for a in store.load_alerts() if a["status"] in daily.OPEN_STATES]
    except store.CorruptFile as e:
        problems.append(f"경고 레지스트리 손상: {e} — data/log/alerts.json.bak 으로 복구 필요")
        alerts = []
    if ev is None:
        return jsonify({"limits": None, "gaps": [], "alerts_open": alerts, "latest_daily": _hide_paths(store.latest_daily()), "mps": {},
                        "pending_checks": store.list_checks()[:10], "api_key_set": api_key_set(), "model": config.MODEL,
                        "data_warnings": _warnings(problems),
                        "log_chain": store.verify_log_chain()})
    gaps = []
    mps = {}
    for asset in ("bond", "equity", "fx"):
        adopted = store.load_mp(asset, adopted_only=True)
        latest = store.load_mp(asset)
        mps[asset] = {"adopted": {k: adopted.get(k) for k in ("mp_id", "status", "created_at", "data_as_of", "valid_to")} if adopted else None,
                      "latest": {k: latest.get(k) for k in ("mp_id", "status", "created_at")} if latest else None,
                      "summary": (adopted or latest or {}).get("producer", {}).get("summary") if (adopted or latest) else None,
                      "verdict": (adopted or latest or {}).get("comparison", {}).get("verdict") if (adopted or latest) else None}
        if adopted:
            gaps += limits.gap_analysis(ev, {asset: adopted}, band_pp=lim.get("mp_band_pp", 2.0))
    gaps.sort(key=lambda r: -abs(r["gap_pp"] or 0))
    latest_daily = _hide_paths(store.latest_daily())
    pending = [c for c in store.list_checks() if (c.get("decision") or {}).get("status") == "PENDING"]
    market = load_json(config.DATA_DIR / "market.json") or load_json(config.DATA_DIR / "market.sample.json")
    return jsonify({
        "limits": ev, "limits_config": {k: lim.get(k) for k in ("total_limit_bn", "asset_limits_bn", "stages", "meta")},
        "gaps": gaps, "alerts_open": alerts, "latest_daily": latest_daily, "mps": mps, "pending_checks": pending,
        "api_key_set": api_key_set(), "model": config.MODEL,
        "data_warnings": _warnings(problems),
        "market_as_of": (market or {}).get("meta", {}).get("generated_at"),
        "log_chain": store.verify_log_chain(),
        "data_feed": _data_feed(),
    })


def _data_feed() -> dict:
    """상단 표시용 — Refinitiv 연결(5분 캐시)과 마지막 자동 갱신 결과(refresh_all.py)·데이터 점검(datahealth.py)."""
    import refinitiv_src
    refinitiv_src.available()
    st = refinitiv_src.status()
    last = load_json(config.DATA_DIR / "log" / "refresh_status.json") or {}
    h = load_json(config.DATA_DIR / "log" / "data_health.json")  # datahealth.py — 사내 공유 접속엔 건수만
    iss = (h or {}).get("issues") or []
    health = {"checked_at": h.get("checked_at"), "n_warn": sum(i.get("severity") == "warn" for i in iss),
              "n_info": sum(i.get("severity") == "info" for i in iss), "issues": iss[:20] if _is_local() else []} if h else None
    return {"refinitiv_ok": bool(st.get("ok")), "refinitiv_error": st.get("error") if _is_local() else ("미연결" if not st.get("ok") else ""),
            "last": {k: last.get(k) for k in ("started_at", "finished_at", "ok", "failed", "error")} if last else None,
            "health": health}


@app.post("/api/daily/run")
def api_daily_run():
    try:
        return jsonify({"job_id": start_job("daily", daily.run_daily)})
    except RuntimeError as e:
        return _err(str(e), 409)


# ── 에이전트 룸 ────────────────────────────────────────────────
import voice as voice_mod  # noqa: E402

ROOM = {"session": None, "session_as_of": None}


def _room_session():
    """대화 세션 — 포지션 기준일·경고 수가 바뀌면 다시 만든다(오늘의 담당 현황을 갱신)."""
    key = (store.latest_daily() or {}).get("ran_at")
    if ROOM["session"] is None or ROOM["session_as_of"] != key:
        ROOM["session"] = voice_mod.new_session()
        ROOM["session_as_of"] = key
    return ROOM["session"]


@app.get("/api/room")
def api_room():
    d = voice_mod.gather()
    latest = d["latest_daily"]
    today = datetime.date.today().isoformat()
    voices = (latest or {}).get("voices") if latest and latest.get("day") == today else None
    if not voices or "error" in voices:
        voices = voice_mod.speak_all(d=d, use_api=False)  # 즉시 표시용 템플릿 대사
    agents = []
    for a in config.AGENTS:
        pos = voice_mod.my_positions(a, d["ev"])
        al = voice_mod.my_alerts(a, d["ev"], d["alerts_open"])
        u = d["ev"]["by_asset"][a]
        agents.append({
            "asset": a, "persona": config.AGENT_PERSONAS[a], "status": {"util_pct": u["util_pct"], "stage": u["stage"], "pnl_bn": u["pnl_bn"], "limit_bn": u["limit_bn"]},
            "voice": {k: v for k, v in voices.get(a, {}).items() if k != "context"},
            "watching": {"positions": len(pos), "mv_bn": round(sum(p["mv_bn"] for p in pos), 0),
                         "alerts": len(al), "pending_checks": sum(1 for c in d["pending"] if c.get("asset") == a),
                         "mp": d["mps"][a]["mp_id"] if d["mps"].get(a) else None,
                         "stale": [p["name"] for p in pos if p["stale"]]},
            "alerts": al,
            "facts": voice_mod.agent_facts(a, d),
        })
    return jsonify({"agents": agents, "as_of": d["ev"]["as_of"], "risk_floor": d["ev"]["risk_floor"],
                    "total": d["ev"]["total"], "api_key_set": api_key_set(),
                    "latest_daily": {k: latest.get(k) for k in ("day", "ran_at")} if latest else None,
                    "voices_source": next(iter(voices.values()), {}).get("source") if voices else None})


@app.post("/api/room/refresh")
def api_room_refresh():
    """오늘의 보고 재생성 (키 있으면 Claude 발언)."""
    def _run():
        d = voice_mod.gather()
        voices = voice_mod.speak_all(d=d, use_api=True)
        latest = store.latest_daily()
        today = datetime.date.today().isoformat()
        if latest and latest.get("day") == today:
            latest["voices"] = voices
            store.save_daily(today, latest)
        return {a: v.get("headline") for a, v in voices.items()}
    return jsonify({"job_id": start_job("voices", _run)})


@app.post("/api/room/talk")
def api_room_talk():
    b = request.get_json(silent=True) or {}
    target = b.get("target") or "all"
    q = (b.get("question") or "").strip()
    if target not in ("all", "bond", "equity", "fx") or not q or len(q) > 2000:
        return _err("대상 또는 질문이 잘못됨 (2000자 이내)")
    sc = b.get("screen") if isinstance(b.get("screen"), dict) else None
    screen = {"tab": str(sc.get("tab") or "")[:20], "label": str(sc.get("label") or "")[:30],
              "text": str(sc.get("text") or "")[:6000]} if sc else None
    try:
        res = voice_mod.talk_room(target, q, _room_session(), screen)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return _err(f"대화 실패: {e}", 502)
    store.append_log("ROOM_TALK", {"target": target, "question": q[:200], "agents": [r["asset"] for r in res["replies"]]})
    return jsonify(res)


@app.post("/api/room/action")
def api_room_action():
    """에이전트가 제안한 행동 실행 → 백그라운드 작업."""
    b = request.get_json(silent=True) or {}
    typ, asset = b.get("type"), b.get("asset")
    if typ == "check":
        trig = b.get("trigger") or "LIMIT_APPROACH"
        if trig not in checks.TRIGGERS or asset not in config.AGENTS:
            return _err("trigger/asset 오류")
        note = b.get("note") or "에이전트 제안으로 실행"
        from agent import share_refinitiv
        if trig == "SHOCK" and not share_refinitiv():
            note = "에이전트 제안으로 실행"   # 급변 메모엔 Refinitiv 수치가 들 수 있다 — 화면이 보낸 메모는 믿지 않는다
        return jsonify({"job_id": start_job("check", checks.run_check, trig, asset, b.get("inputs") or {}, None, note)})
    if typ == "mp":
        if not api_key_set():
            return _err("MP 산출은 API 키가 필요합니다", 503)
        if asset not in config.AGENTS:
            return _err("asset 오류")
        return jsonify({"job_id": start_job("mp", mp_mod.generate_mp, asset)})
    if typ == "discuss":
        if not api_key_set():
            return _err("토의는 API 키가 필요합니다", 503)
        from council import run_discussion
        try:
            return jsonify({"job_id": start_job("discuss", run_discussion, b.get("note") or None)})
        except RuntimeError as e:
            return _err(str(e), 409)
    if typ == "fetch":
        import fetch_market
        return jsonify({"job_id": start_job("fetch", fetch_market.main)})
    return _err("알 수 없는 행동")


@app.get("/api/discussions")
def api_discussions():
    items = []
    for p in sorted(config.REPORT_DIR.glob("*_국면토의.json"), reverse=True)[:20]:
        d = store.read_json(p)
        if d:
            items.append({"name": p.name, "date": d.get("date"), "topic": d.get("topic"), "minutes": d.get("minutes")})
    return jsonify(items)


@app.get("/api/discussions/<name>")
def api_discussion(name):
    if name != Path(name).name or not name.endswith("_국면토의.json"):
        return _err("잘못된 파일명")
    d = store.read_json(config.REPORT_DIR / name)
    if not d:
        return _err("없음", 404)
    return jsonify(d)


# ── 경고 ──────────────────────────────────────────────────────
@app.get("/api/alerts")
def api_alerts():
    reg = store.load_alerts()
    order = {"OPEN": 0, "ACTIONED": 1, "CONFIRMED": 2, "CLOSED": 3}
    reg.sort(key=lambda a: (order.get(a["status"], 9), -limits.STAGE_ORDER.get(a["grade"], 0), a.get("opened_at", "")))
    return jsonify(reg)


@app.post("/api/alerts/<alert_id>/action")
def api_alert_action(alert_id):
    b = request.get_json(silent=True) or {}
    try:
        a = daily.act_alert(alert_id, b.get("status", ""), (b.get("by") or "운용역")[:40], (b.get("note") or "")[:500],
                            float(b["amount_bn"]) if b.get("amount_bn") not in (None, "") else None)
    except KeyError:
        return _err("경고 없음", 404)
    except ValueError as e:
        return _err(str(e), 400)
    return jsonify(a)


# ── MP ────────────────────────────────────────────────────────
@app.get("/api/mp")
def api_mp_list():
    out = []
    for m in store.list_mps():
        out.append({k: m.get(k) for k in ("mp_id", "asset", "asset_name", "status", "created_at", "data_as_of", "valid_to")}
                   | {"summary": m["producer"]["summary"], "verdict": m["comparison"]["verdict"], "confidence": m["producer"]["confidence"]})
    out.sort(key=lambda m: m["created_at"], reverse=True)
    return jsonify(out)


@app.get("/api/mp/<mp_id>")
def api_mp_get(mp_id):
    doc = next((m for m in store.list_mps() if m["mp_id"] == mp_id), None)
    if not doc:
        return _err("MP 없음", 404)
    return jsonify({"doc": doc, "markdown": mp_mod.to_markdown(doc)})


@app.post("/api/mp/generate")
def api_mp_generate():
    if not api_key_set():
        return _err("ANTHROPIC_API_KEY 미설정 — MP 산출은 Claude 호출이 필요합니다.", 503)
    asset = (request.get_json(silent=True) or {}).get("asset")
    if asset not in ("bond", "equity", "fx"):
        return _err("asset 값 오류")
    return jsonify({"job_id": start_job("mp", mp_mod.generate_mp, asset)})


@app.post("/api/mp/<mp_id>/adopt")
def api_mp_adopt(mp_id):
    by = ((request.get_json(silent=True) or {}).get("by") or "운용역")[:40]
    try:
        return jsonify(mp_mod.adopt_mp(mp_id, by))
    except KeyError:
        return _err("MP 없음", 404)


@app.get("/api/analog")
def api_analog():
    import history
    asset = request.args.get("asset", "bond")
    if asset not in ("bond", "equity", "fx"):
        return _err("asset 값 오류")
    try:
        res = history.find_analogs(asset)
    except Exception as e:  # noqa: BLE001
        return _err(f"과거 사례 계산 불가: {e}", 500)
    return jsonify({"result": res, "markdown": history.to_markdown(res)})


# ── 점검 보고서 ────────────────────────────────────────────────
@app.get("/api/checks")
def api_checks():
    return jsonify(store.list_checks())


@app.get("/api/checks/<report_id>")
def api_check_get(report_id):
    doc = store.load_check(report_id)
    if not doc:
        return _err("보고서 없음", 404)
    return jsonify({"doc": doc, "markdown": checks.to_markdown(doc)})


@app.post("/api/checks")
def api_check_create():
    b = request.get_json(silent=True) or {}
    trigger, asset = b.get("trigger"), b.get("asset")
    if trigger not in checks.TRIGGERS or asset not in ("bond", "equity", "fx"):
        return _err("trigger/asset 값 오류")
    inputs = checks.sanitize_inputs(b.get("inputs") if isinstance(b.get("inputs"), dict) else {})
    if trigger == "NEW":
        try:
            if float(inputs.get("mv_bn") or 0) <= 0:
                return _err("신규 투자 금액(억원)을 입력하세요")
        except (TypeError, ValueError):
            return _err("금액 형식 오류")
    note = (b.get("note") or "")[:500]
    return jsonify({"job_id": start_job("check", checks.run_check, trigger, asset, inputs, None, note)})


@app.post("/api/checks/<report_id>/decision")
def api_check_decision(report_id):
    b = request.get_json(silent=True) or {}
    try:
        doc = checks.decide(report_id, b.get("status", ""), (b.get("by") or "운용역")[:40], (b.get("reason") or "")[:500])
    except KeyError:
        return _err("보고서 없음", 404)
    except ValueError as e:
        return _err(str(e), 400)
    return jsonify(doc["decision"])


# ── 로그·보고서·원본 데이터 ─────────────────────────────────────
@app.get("/api/log")
def api_log():
    limit = min(int(request.args.get("limit", 200)), 2000)
    return jsonify({"records": list(reversed(store.read_log(limit=limit))), "chain": store.verify_log_chain()})


@app.post("/api/monthly")
def api_monthly():
    import re
    month = (request.get_json(silent=True) or {}).get("month") or datetime.date.today().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return _err("month 형식 YYYY-MM")
    return jsonify({"month": month, "markdown": daily.monthly_report(month)})


@app.get("/api/positions")
def api_positions():
    return jsonify({"positions": limits.load_positions(), "limits": limits.load_limits(),
                    "is_sample": {"positions": limits.is_sample("positions"), "limits": limits.is_sample("limits")}})


# ── 내 포트 (한 화면 요약) ──────────────────────────────────────
GROUP_AGENT = {"주식": "equity", "채권": "bond", "환": "fx"}
ENGINE_GROUP = {"equity": "주식", "bond": "채권", "mixed": "채권", "fx": "환"}


@app.get("/api/book")
def api_book():
    doc = limits.load_positions()
    lim = limits.load_limits()
    ev = limits.evaluate(positions_doc=doc, limits=lim)
    rows = {r["id"]: r for r in ev["positions"]}
    pf = load_json(config.DATA_DIR / "portfolio.json") or {}
    active = pf.get("book") or {}
    out = []
    for p in doc.get("positions", []):
        r = rows.get(p["id"], {})
        group = p.get("group") or ENGINE_GROUP.get(p.get("asset"), "기타")
        is_fx = p.get("asset") == "fx"
        pnl = float(p.get("pnl_ytd_bn") or 0) + float(p.get("pnl_fx_ytd_bn") or 0)
        base = float(p.get("fx_exposure_bn") or 0) if is_fx else float(p.get("base_bn") or p.get("mv_bn") or 0)
        short = sum(l.get("mv_bn", 0) for l in p.get("legs", []) if l.get("side") == "숏")
        fx = r.get("fx") or {}
        out.append({
            "id": p["id"], "name": p.get("short_name") or p["name"], "full_name": p["name"], "group": group,
            "agent": GROUP_AGENT.get(group), "strategy": p.get("strategy") or p.get("sub"), "management": p.get("management"),
            "currency": p.get("currency", "KRW"), "hedge_pct": p.get("hedge_pct"),
            "mv_bn": float(p.get("fx_exposure_bn") or 0) if is_fx else float(p.get("mv_bn") or 0),
            "short_bn": round(short, 1), "pnl_bn": round(pnl, 2), "ret_pct": round(pnl / base * 100, 2) if base else 0.0,
            "stop_pct": p.get("fx_stop_pct") if is_fx else p.get("stop_loss_pct"), "target_pct": p.get("target_pct"),
            "stop_ratio_pct": fx.get("stop_ratio_pct", 0) if is_fx else r.get("stop_ratio_pct", 0),
            "stage": (fx.get("stage") if is_fx else r.get("stage")) or "NORMAL",
            "pnl_as_of": p.get("pnl_as_of"), "stale": r.get("stale", False), "is_fx": is_fx, "legs": p.get("legs", []),
        })
    strat = {}
    for o in out:
        if not o["is_fx"]:
            strat[o["strategy"]] = strat.get(o["strategy"], 0) + o["mv_bn"]
    mix = active.get("strategy_mix_pct", {})
    goal = {"마켓타이밍": mix.get("market_timing"), "상대가치": mix.get("relative_value"), "이벤트 드리븐": mix.get("event_driven")}
    scoring = [s for s in ev["stress"] if not s["sizing"] and not s["reference"]]
    worst = max(scoring, key=lambda s: s["loss_total_bn"]) if scoring else None
    meta = doc.get("meta", {})
    import quant   # is_trial 판정 한 곳(quant.is_trial)
    return jsonify({
        "meta": {k: meta.get(k) for k in ("as_of", "source", "note", "price_dates", "proxies")},
        "is_sample": limits.is_sample("positions"), "is_trial": quant.is_trial(meta),
        "target": {"size_bn": active.get("size_bn")},
        "totals": {"mv_bn": round(sum(o["mv_bn"] for o in out if not o["is_fx"]), 1),
                   "fx_notional_bn": round(sum(o["mv_bn"] for o in out if o["is_fx"]), 1),
                   "pnl_bn": round(sum(o["pnl_bn"] for o in out), 2), "n": len(out)},
        "total": ev["total"], "by_asset": ev["by_asset"], "risk_floor": ev["risk_floor"],
        "stress": [{k: s[k] for k in ("id", "name", "loss_total_bn", "post_util_total_pct", "post_stage_total")}
                   for s in sorted(scoring, key=lambda s: -s["loss_total_bn"])[:4]],
        "worst_stress": {k: worst[k] for k in ("name", "loss_total_bn", "post_util_total_pct", "post_stage_total")} if worst else None,
        "strategies": [{"name": k, "long_bn": round(v, 1), "target_pct": goal.get(k)} for k, v in
                       sorted(strat.items(), key=lambda kv: -kv[1])],
        "positions": out,
    })


@app.get("/api/quant")
def api_quant():
    import quant
    d = quant.load()
    if d is None:
        if not _is_local():
            return _err("정량 분석 결과가 아직 없습니다 — 운영 PC 에서 먼저 계산하세요.", 503)
        try:
            d = quant.run()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return _err(f"정량 분석 실패: {e}", 500)
    return jsonify(d)


@app.get("/api/strategy")
def api_strategy():
    """전략 보드 — 포지션·한도·정량 결과로 지금 할 일을 규칙(CODE)으로 정리(가벼워서 요청마다 다시 계산)."""
    import strategy
    return jsonify(strategy.build(save=_is_local()))


@app.get("/api/equity")
def api_equity():
    """주식 대시보드 — 마지막 계산값을 바로 주고, 6시간 넘었으면 백그라운드에서 다시 받는다."""
    import equity
    d = equity.load() or {}
    age = equity.age_hours()
    running = any(j["kind"] == "equity" and j["status"] == "running" for j in JOBS.values())
    if (age is None or age > 6) and not running and _is_local():   # 원격 조회로는 수집을 시작하지 않는다
        start_job("equity", equity.build)
        running = True
    d["refreshing"] = running
    import backtest
    d["bt_summary"] = backtest.load_summary()   # 유니버스·전략 표에 '연 수익률·낙폭'을 붙이는 데 쓴다
    return jsonify(d)


@app.get("/api/universe/spark")
def api_universe_spark():
    """홈 점수판 차트 — 주식 유니버스·정량 스캐너의 1년 일별 값과 200일선(읽기 전용, 원본 파일이 바뀔 때만 다시 계산)."""
    import equity
    return jsonify(equity.sparks())


@app.post("/api/equity/refresh")
def api_equity_refresh():
    import equity
    if any(j["kind"] == "equity" and j["status"] == "running" for j in JOBS.values()):
        return _err("이미 받는 중입니다.", 409)
    return jsonify({"job_id": start_job("equity", equity.build)})


@app.get("/api/equity/fund")
def api_equity_fund():
    """유니버스 밸류에이션(PER·EPS·상위 10종목) — 마지막 값을 바로 주고, 12시간 넘었으면 백그라운드에서 다시 받는다."""
    import fundamentals
    d = fundamentals.load() or {}
    age = fundamentals.age_hours()
    running = [j for j in JOBS.values() if j["kind"] == "fund" and j["status"] == "running"]
    job_id = running[0]["job_id"] if running else None
    if (age is None or age > fundamentals.STALE_HOURS) and not running and _is_local():
        job_id = start_job("fund", fundamentals.build)
    d["refreshing"], d["job_id"] = job_id is not None, job_id
    return jsonify(d)


@app.get("/api/equity/valhist")
def api_equity_valhist():
    """유니버스 항목별 자기 과거 PER·PBR 월말 이력(valhist.py) — 없으면 빈 items."""
    import valhist
    return jsonify(valhist.load() or {"generated_at": None, "method": None, "items": {}})


@app.post("/api/equity/fund/refresh")
def api_equity_fund_refresh():
    import fundamentals
    if any(j["kind"] == "fund" and j["status"] == "running" for j in JOBS.values()):
        return _err("이미 받는 중입니다.", 409)
    return jsonify({"job_id": start_job("fund", fundamentals.build)})


@app.get("/api/backtest/catalog")
def api_bt_catalog():
    import backtest
    try:
        d = backtest.catalog()
    except ValueError as e:   # 가격 파일이 아직 없음 — 받는 방법을 안내(데이터 준비 전이라 503)
        return _err(str(e), 503)
    d["summary"] = backtest.load_summary()
    return jsonify(d)


@app.post("/api/backtest/run")
def api_bt_run():
    """한 전략 백테스트 — 빨라서(1초 안팎) 바로 돌려 결과를 준다."""
    import backtest
    body = request.get_json(silent=True) or {}
    remote = not _is_local()
    if remote and not REMOTE_BT.acquire(blocking=False):
        return _err("다른 백테스트가 돌고 있습니다 — 잠시 뒤 다시 누르세요.", 409)
    try:
        return jsonify(backtest.run(str(body.get("id", "")), body.get("params") or {}, str(body.get("period") or "10y"),
                                    body.get("start") or None, body.get("end") or None))
    except ValueError as e:
        return _err(str(e), 400)
    finally:
        if remote:
            REMOTE_BT.release()


@app.post("/api/backtest/all")
def api_bt_all():
    """모든 전략을 기본값으로 — '전략별 기대수익과 리스크' 표. 결과는 data/backtest_summary.json 에 남는다."""
    import backtest
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(backtest.run_all(str(body.get("period") or "10y"), body.get("start") or None, body.get("end") or None))
    except ValueError as e:
        return _err(str(e), 400)


@app.post("/api/quant/run")
def api_quant_run():
    import quant
    try:
        return jsonify({"job_id": start_job("quant", quant.run)})
    except RuntimeError as e:
        return _err(str(e), 409)


@app.get("/api/macro")
def api_macro():
    """마지막 값을 바로 돌려주고, 오래됐으면(INFOMAX_REFRESH_MIN) 백그라운드에서 숨김 엑셀로 다시 받는다."""
    import infomax_bridge
    # 원격(사내 공유) 화면이 이 PC 에서 숨김 엑셀을 띄우지 않게 — 자동 갱신은 이 PC 에서 볼 때만
    refreshing = (infomax_bridge.refresh_if_stale(config.INFOMAX_REFRESH_MIN)
                  if infomax_bridge.BRIDGE.exists() and _is_local() else False)
    doc = infomax_bridge.load()
    doc["refreshing"] = refreshing
    doc["bridge_exists"] = infomax_bridge.BRIDGE.exists()
    doc["bridge_path"] = str(infomax_bridge.BRIDGE) if _is_local() else ""
    meta = doc.get("meta")
    if not _is_local() and isinstance(meta, dict) and meta.get("workbook"):   # 원격에는 이 PC 폴더 경로 대신 파일 이름만
        from pathlib import PureWindowsPath
        meta["workbook"] = PureWindowsPath(str(meta["workbook"])).name
    doc["share_vendor"] = config.SHARE_VENDOR_DATA
    doc["refresh_min"] = config.INFOMAX_REFRESH_MIN
    return jsonify(doc)


@app.post("/api/macro/refresh")
def api_macro_refresh():
    """지금 바로 읽기(열린 브리지 → 숨김 엑셀 → 마지막 값). 엑셀 COM 호출이 겹치지 않게 한 번에 하나."""
    import infomax_bridge
    try:
        doc = infomax_bridge.read()
    except RuntimeError as e:
        return _err(str(e), 409)
    doc["refreshing"] = False
    doc["bridge_exists"] = infomax_bridge.BRIDGE.exists()
    doc["bridge_path"] = str(infomax_bridge.BRIDGE)
    doc["share_vendor"] = config.SHARE_VENDOR_DATA
    doc["refresh_min"] = config.INFOMAX_REFRESH_MIN
    return jsonify(doc)


@app.post("/api/book/mark")
def api_book_mark():
    import book
    if not book.DEFAULT_BOOK.exists():
        return _err("보유 엑셀(data/시험포트.xlsx)이 없습니다.", 404)
    def _mark():   # 미연결·가격 0건이면 쓰지 않고 skipped — 작업을 실패로 끝내 화면이 '완료'로 오해하지 않게
        r = book.sync(quiet=True)
        if r.get("skipped"):
            raise RuntimeError(r.get("error") or "시가 평가를 건너뜀 — 기존 positions.json 유지")
        return r

    try:
        return jsonify({"job_id": start_job("book", _mark)})
    except RuntimeError as e:
        return _err(str(e), 409)


# ── 기존: 시장·포트 패널, 채팅, 토의, 회의록 ─────────────────────
@app.get("/api/state")
def api_state():
    market = load_json(config.DATA_DIR / "market.json") or load_json(config.DATA_DIR / "market.sample.json")
    reports = sorted((p.name for p in config.REPORT_DIR.glob("*.md")), reverse=True)[:60] if config.REPORT_DIR.exists() else []
    disc = next((j for j in sorted(JOBS.values(), key=lambda j: j["started_at"], reverse=True) if j["kind"] == "discuss"), None)
    return jsonify({"market": market, "reports": reports, "api_key_set": api_key_set(), "model": config.MODEL,
                    "discuss": {"status": disc["status"] if disc else "idle", "report": os.path.basename(disc["result"]) if disc and disc["status"] == "done" else None,
                                "error": disc["error"] if disc else None},
                    "data_warnings": _warnings()})


@app.post("/api/ask")
def api_ask():
    if not api_key_set():
        return _err("ANTHROPIC_API_KEY 미설정 — console.anthropic.com 에서 키 발급 후 setx 로 등록하고 서버를 재시작하세요.", 503)
    b = request.get_json(silent=True) or {}
    q = (b.get("question") or "").strip()
    agent = b.get("agent") or "auto"
    if not q or len(q) > 2000:
        return _err("질문이 비었거나 너무 깁니다 (2000자 이내)")
    try:
        if CHAT["state"] is None:
            CHAT["state"] = qa.new_state()
        r = qa.answer_question(q, CHAT["state"], targets=None if agent == "auto" else [agent])
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return _err(f"API 호출 실패: {e}", 502)
    return jsonify({"targets": r["targets"], "answers": [{"agent": n, "name": config.AGENT_NAMES[n], "text": t} for n, t in r["answers"].items()], "summary": r["summary"]})


@app.post("/api/fetch")
def api_fetch():
    import fetch_market
    if not FETCH_LOCK.acquire(blocking=False):
        return _err("이미 갱신 중입니다.", 409)
    try:
        fetch_market.main()
        CHAT["state"] = None
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return _err(f"시장 데이터 갱신 실패: {e}", 502)
    finally:
        FETCH_LOCK.release()
    return jsonify({"ok": True})


@app.post("/api/discuss")
def api_discuss():
    if not api_key_set():
        return _err("ANTHROPIC_API_KEY 미설정", 503)
    from council import run_discussion
    topic = (request.get_json(silent=True) or {}).get("topic") or None
    try:
        return jsonify({"job_id": start_job("discuss", run_discussion, topic)})
    except RuntimeError as e:
        return _err(str(e), 409)


@app.get("/api/report/<name>")
def api_report(name):
    if name != Path(name).name or any(c in name for c in ("/", "\\", ":", "..")) or not name.endswith(".md"):
        return _err("잘못된 파일명")
    path = (config.REPORT_DIR / name).resolve()
    if not path.is_relative_to(config.REPORT_DIR.resolve()) or not path.exists():
        return _err("파일 없음", 404)
    return jsonify({"name": name, "content": path.read_text(encoding="utf-8")})


if __name__ == "__main__":
    print("AI 운용관리 대시보드: http://127.0.0.1:5101")
    if not api_key_set():
        print("주의: ANTHROPIC_API_KEY 미설정 — 한도·신호·괴리(CODE)만 동작, MP·점검 해석·채팅은 비활성.")
    host = "127.0.0.1"
    if config.LAN_SHARE:
        _lan_setup()
        host = "0.0.0.0"
        print(f"사내 공유 켜짐(읽기 전용, 허용 대역 {', '.join(config.LAN_ALLOW)}) — 동료에게 줄 링크:")
        print(f"  http://{LAN['ip']}:5101/?key={LAN['key']}")
    app.run(host=host, port=5101, debug=False, threaded=True)
