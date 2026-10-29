# -*- coding: utf-8 -*-
"""마켓타이밍 점수(mtscore) 읽기 — 계산은 하지 않고, mtscore 가 만든 최신 결과 JSON 을 화면용으로 줄여 돌려준다.

연결 설정(결과 폴더·대시보드 키 대응·표시 이름·순서)은 universe/mtscore.json 에 있다. 바꿀 때는 그 파일만 고친다.
결과 폴더는 환경변수 MTSCORE_RESULT_DIR 로 덮어쓸 수 있다(운영 PC 경로가 다를 때).
"""
import json
import os
import re
from pathlib import Path

import universe

CFG = universe.DIR / "mtscore.json"
_NAME = re.compile(r"^result_(\d{4}-\d{2}-\d{2})\.json$")
_CACHE: dict = {}


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def result_dir(cfg: dict) -> Path:
    env = os.environ.get("MTSCORE_RESULT_DIR")
    return Path(env) if env else (universe.DIR / cfg["meta"]["result_dir"]).resolve()


def latest_file(d: Path) -> Path | None:
    """result_YYYY-MM-DD.json 중 날짜가 가장 늦은 파일. 없으면 None."""
    files = sorted((p for p in d.glob("result_*.json") if _NAME.match(p.name)), key=lambda p: p.name)
    return files[-1] if files else None


def _slim(a: dict) -> dict:
    """화면에 필요한 필드만 — contributions(지표별 상세)는 뺀다."""
    keep = ("asset_id", "date", "stale", "blocks", "bands", "regime", "staged_entry", "trend", "diffusion")
    return {k: a.get(k) for k in keep}


def view(cfg: dict | None = None) -> dict:
    """{as_of, schema_version, file, assets: {asset_id: 요약}, map, names, order, note}. 결과 파일이 없으면 FileNotFoundError."""
    cfg = cfg or load_cfg()
    d = result_dir(cfg)
    f = latest_file(d) if d.is_dir() else None
    if f is None:
        raise FileNotFoundError(f"mtscore 결과 파일 없음: {d}")
    key = (str(f), f.stat().st_mtime, CFG.stat().st_mtime)
    if _CACHE.get("key") == key:
        return _CACHE["d"]
    r = json.loads(f.read_text(encoding="utf-8"))
    out = {"as_of": r.get("as_of"), "schema_version": r.get("schema_version"), "file": f.name,
           "assets": {a["asset_id"]: _slim(a) for a in r.get("assets", [])},
           "map": cfg["map"], "names": cfg.get("names", {}), "order": cfg.get("order", []),
           "note": cfg["meta"].get("as_of_note", ""), "unverified": cfg.get("unverified", [])}
    _CACHE.update(key=key, d=out)
    return out
