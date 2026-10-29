# -*- coding: utf-8 -*-
"""mtscore_view — 최신 결과 파일 고르기, 상세 제거, 연결 설정, 파일 없음."""
import json

import pytest

import mtscore_view


def _cfg(tmp_path):
    return {"meta": {"result_dir": str(tmp_path), "as_of_note": "n"}, "map": {"q:gold": {"asset": "GOLD"}},
            "names": {"GOLD": "금"}, "order": ["GOLD"]}


def _write(p, as_of, regime="중립"):
    a = {"asset_id": "GOLD", "date": as_of, "stale": False, "blocks": {"oversold": 10.0}, "bands": {}, "regime": {"display": regime},
         "staged_entry": {"flag": False}, "trend": {"sign": 1}, "diffusion": {"n_high": 1}, "contributions": [{"x": 1}]}
    p.write_text(json.dumps({"schema_version": "1.3", "as_of": as_of, "assets": [a]}), encoding="utf-8")


def test_picks_latest_and_drops_contributions(tmp_path, monkeypatch):
    monkeypatch.setenv("MTSCORE_RESULT_DIR", str(tmp_path))
    _write(tmp_path / "result_2026-10-20.json", "2026-10-20", "옛것")
    _write(tmp_path / "result_2026-10-27.json", "2026-10-27", "새것")
    (tmp_path / "result_latest_backup.json").write_text("{}", encoding="utf-8")   # 이름 규칙 밖 → 무시
    mtscore_view._CACHE.clear()
    v = mtscore_view.view(_cfg(tmp_path))
    assert v["as_of"] == "2026-10-27" and v["file"] == "result_2026-10-27.json"
    g = v["assets"]["GOLD"]
    assert g["regime"]["display"] == "새것" and "contributions" not in g
    assert v["map"] == {"q:gold": {"asset": "GOLD"}} and v["order"] == ["GOLD"]


def test_missing_dir_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MTSCORE_RESULT_DIR", str(tmp_path / "none"))
    mtscore_view._CACHE.clear()
    with pytest.raises(FileNotFoundError):
        mtscore_view.view(_cfg(tmp_path))


def test_real_config_maps_known_assets():
    cfg = mtscore_view.load_cfg()
    assets = {m["asset"] for m in cfg["map"].values()}
    assert assets <= set(cfg["order"])
    assert set(cfg["order"]) <= set(cfg["names"])
