# -*- coding: utf-8 -*-
"""산출물 저장소 — MP, 점검 보고서, 경고 레지스트리, 로그(해시 체인), 일일 스냅샷.

동시성: 웹 서버는 백그라운드 작업 스레드 + HTTP 스레드가 함께 돈다. 모든 read-modify-write
(로그 append, 레지스트리 갱신, ID 발급)는 모듈 전역 RLock 안에서 수행한다(단일 프로세스 전제 —
스케줄러는 별도 프로세스 대신 서버의 /api/daily/run 을 호출하게 안내).
로그 무결성: 레코드마다 prev_hash→hash 체인 + head.json(마지막 해시·건수) 앵커로 꼬리 절단을 잡는다.
LOG_HMAC_KEY 환경변수가 있으면 HMAC-SHA256 으로 서명해 파일 접근만으로는 재작성이 안 되게 한다.
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
from pathlib import Path

import config

MP_DIR = config.DATA_DIR / "mp"
CHECK_DIR = config.DATA_DIR / "checks"
LOG_DIR = config.DATA_DIR / "log"
DAILY_DIR = LOG_DIR / "daily"
LOG_FILE = LOG_DIR / "events.jsonl"
HEAD_FILE = LOG_DIR / "head.json"
ALERTS_FILE = LOG_DIR / "alerts.json"

LOCK = threading.RLock()
_seq = 0


def _ensure():
    for d in (MP_DIR, CHECK_DIR, LOG_DIR, DAILY_DIR, config.REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _clean(obj):
    """NaN/inf → None (JSON 표준 위반 방지)."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def sha(obj) -> str:
    raw = json.dumps(_clean(obj), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    key = os.environ.get("LOG_HMAC_KEY")
    if key:
        return "hmac:" + hmac.new(key.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:16]
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def prompt_version() -> str:
    h = hashlib.sha256()
    for p in sorted(config.PROMPT_DIR.glob("*.md")):
        h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()[:8]


def save_json(path: Path, obj) -> Path:
    _ensure()
    with LOCK:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(_clean(obj), ensure_ascii=False, indent=1, default=str, allow_nan=False), encoding="utf-8")
        tmp.replace(path)
    return path


class CorruptFile(RuntimeError):
    pass


def read_json(path: Path, default=None, strict: bool = False):
    """strict=True 면 파싱 실패를 CorruptFile 로 올린다(없는 파일은 default)."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        if strict:
            raise CorruptFile(f"{path.name}: {e}") from e
        return default


def new_id(prefix: str, directory: Path) -> str:
    """기존 파일명의 최대 순번+1 을 락 안에서 발급하고 빈 자리표시 파일로 예약한다."""
    _ensure()
    day = datetime.date.today().strftime("%Y%m%d")
    with LOCK:
        mx = 0
        for p in directory.glob(f"{prefix}-{day}-*.json"):
            m = re.search(rf"{prefix}-{day}-(\d+)\.json$", p.name)
            if m:
                mx = max(mx, int(m.group(1)))
        rid = f"{prefix}-{day}-{mx + 1:03d}"
        (directory / f"{rid}.json").write_text("{}", encoding="utf-8")  # 예약 (list_* 는 빈 문서를 건너뜀)
    return rid


def meta_block(pass_name: str, inputs: dict, model: str | None = None) -> dict:
    return {"model_id": model or config.MODEL, "prompt_version": prompt_version(),
            "inputs_hash": sha(inputs), "generated_at": now_iso(), "pass": pass_name}


# ── 로그 (해시 체인 + head 앵커) ─────────────────────────────────
def _read_head() -> dict:
    return read_json(HEAD_FILE, default={"hash": "genesis", "count": 0}) or {"hash": "genesis", "count": 0}


def append_log(kind: str, payload: dict) -> dict:
    global _seq
    _ensure()
    with LOCK:
        head = _read_head_or_rebuild()
        _seq += 1
        rec = {"log_id": f"LOG-{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}-{_seq:04d}",
               "ts": now_iso(), "kind": kind, **payload, "prev_hash": head["hash"]}
        rec["hash"] = sha(rec)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_clean(rec), ensure_ascii=False, default=str) + "\n")
            f.flush()
        HEAD_FILE.write_text(json.dumps({"hash": rec["hash"], "count": head["count"] + 1, "ts": rec["ts"]}), encoding="utf-8")
    return rec


def read_log(limit: int | None = 200, kinds: set[str] | None = None, since: str | None = None) -> list[dict]:
    recs, bad = _read_all_log()
    out = [r for r in recs if (not kinds or r.get("kind") in kinds) and (not since or r.get("ts", "") >= since)]
    return out[-limit:] if limit else out


def _read_all_log() -> tuple[list[dict], int]:
    if not LOG_FILE.exists():
        return [], 0
    recs, bad = [], 0
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return recs, bad


def verify_log_chain() -> dict:
    with LOCK:
        recs, bad = _read_all_log()
        head = read_json(HEAD_FILE)
    prev = "genesis"
    for i, r in enumerate(recs):
        body = {k: v for k, v in r.items() if k != "hash"}
        ok_prev = r.get("prev_hash") == prev or (i == 0 and r.get("prev_hash") in ("genesis", "sha256:genesis"))
        if not ok_prev or sha(body) != r.get("hash"):
            return {"ok": False, "reason": "체인 불일치", "broken_at": i, "log_id": r.get("log_id"), "count": len(recs)}
        prev = r["hash"]
    if bad:
        return {"ok": False, "reason": f"파싱 불가 줄 {bad}개", "count": len(recs)}
    if recs and head is None:
        return {"ok": True, "count": len(recs), "note": "head 앵커 없음(구버전 로그) — 다음 기록부터 앵커 생성"}
    if recs and head and (head.get("hash") != prev or head.get("count") != len(recs)):
        return {"ok": False, "reason": "head 앵커 불일치(꼬리 절단 또는 재작성 의심)", "count": len(recs), "head": head}
    return {"ok": True, "count": len(recs), "signed": bool(os.environ.get("LOG_HMAC_KEY"))}


def _read_head_or_rebuild() -> dict:
    """head.json 이 없으면(구버전 로그) 현재 파일 끝에서 재구성."""
    head = read_json(HEAD_FILE)
    if head:
        return head
    recs, _ = _read_all_log()
    return {"hash": recs[-1]["hash"] if recs else "genesis", "count": len(recs)}


# ── 경고 레지스트리 (임계구역 helper) ────────────────────────────
def load_alerts() -> list[dict]:
    with LOCK:
        data = read_json(ALERTS_FILE, default=[], strict=True)
    return data or []


def save_alerts(alerts: list[dict]):
    with LOCK:
        if ALERTS_FILE.exists():
            shutil.copyfile(ALERTS_FILE, ALERTS_FILE.with_suffix(".json.bak"))
        save_json(ALERTS_FILE, alerts)


def update_alerts(fn):
    """fn(reg) -> result. 읽기→수정→쓰기를 하나의 임계구역으로."""
    with LOCK:
        reg = load_alerts()
        result = fn(reg)
        save_alerts(reg)
        return result


# ── MP ─────────────────────────────────────────────────────────
def list_mps(asset: str | None = None) -> list[dict]:
    _ensure()
    out = []
    for p in sorted(MP_DIR.glob("MP-*.json")):
        d = read_json(p)
        if d and d.get("mp_id") and (asset is None or d.get("asset") == asset):
            out.append(d)
    return out


def load_mp(asset: str, adopted_only: bool = False) -> dict | None:
    mps = list_mps(asset)
    adopted = [m for m in mps if m.get("status") == "ADOPTED"]
    if adopted:
        return adopted[-1]
    if adopted_only:
        return None
    drafts = [m for m in mps if m.get("status") == "DRAFT"]
    return drafts[-1] if drafts else None


def save_mp(doc: dict) -> Path:
    return save_json(MP_DIR / f"{doc['mp_id']}.json", doc)


# ── 점검 보고서 ────────────────────────────────────────────────
def list_checks() -> list[dict]:
    _ensure()
    out = []
    for p in sorted(CHECK_DIR.glob("CHK-*.json"), reverse=True):
        d = read_json(p)
        if d and d.get("report_id"):
            out.append({k: d.get(k) for k in ("report_id", "trigger", "asset", "created_at", "final_grade",
                                               "verdict", "rule", "decision", "headline", "limit_breach")})
    return out


def load_check(report_id: str) -> dict | None:
    if not re.fullmatch(r"CHK-\d{8}-\d{3,}", report_id or ""):
        return None
    d = read_json(CHECK_DIR / f"{report_id}.json")
    return d if d and d.get("report_id") else None


def save_check(doc: dict) -> Path:
    return save_json(CHECK_DIR / f"{doc['report_id']}.json", doc)


# ── 일일 스냅샷 ────────────────────────────────────────────────
def save_daily(day: str, doc: dict) -> Path:
    return save_json(DAILY_DIR / f"{day}.json", doc)


def load_daily(day: str) -> dict | None:
    return read_json(DAILY_DIR / f"{day}.json")


def list_daily(prefix: str = "") -> list[dict]:
    _ensure()
    return [d for p in sorted(DAILY_DIR.glob(f"{prefix}*.json")) if (d := read_json(p))]


def latest_daily() -> dict | None:
    items = list_daily()
    return items[-1] if items else None
