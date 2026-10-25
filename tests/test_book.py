# -*- coding: utf-8 -*-
"""보유 시가 평가(book.py) 회귀 테스트 — Refinitiv·엑셀·네트워크 없이 돈다(합성 다리·tmp_path).

Workspace 꺼진 날 positions.json 이 손익 0 으로 덮이지 않는지, 가격 못 받은 다리가 표시되는지,
환 오버레이 방향(숏)이 스트레스에 반영되는지, limits.json 이 원자적으로 쓰이는지 고정한다.
"""
import datetime
import json
import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import book  # noqa: E402
import limits  # noqa: E402
import refinitiv_src  # noqa: E402

TODAY = datetime.date.today().isoformat()
GA = {"USD/KRW 진입 환율": 1300.0}


def _leg(pos, code, market, ccy, entry_px, entry_fx, name=None, side="롱", mult=1.0, qty=1e5):
    return {"row": f"{pos}-1", "pos": pos, "name": name or code, "code": code, "market": market, "ccy": ccy,
            "side": side, "entry_px": entry_px, "entry_fx": entry_fx, "mult": mult, "qty": qty, "beta": 1.0, "dur": None}


def _pos(name, engine, ccy, stop=-0.05, target=None):
    return {"name": name, "asset_ko": "주식", "strategy": "s", "how": "직접", "hedge": 0, "target": target, "stop": stop,
            "engine": engine, "ccy": ccy, "sdur": None, "sbeta": None, "eq_pct": None}


def _book_stub():
    legs = [_leg("E1", "069500", "KRX ETF", "KRW", 30000.0, 1.0)]
    return legs, {"E1": _pos("코스피", "equity", "KRW")}, dict(GA)


# ── 오프라인이면 쓰지 않는다 ──
def test_book_sync_offline_keeps_positions(tmp_path, monkeypatch):
    """오프라인이면 가격 조회 없이 기존 positions.json 을 그대로 둔다."""
    monkeypatch.setattr(refinitiv_src, "available", lambda force=False: False)
    monkeypatch.setattr(book, "fetch_prices", lambda s: pytest.fail("오프라인인데 가격 조회"))
    monkeypatch.setattr(book, "read_book", lambda p: _book_stub())
    monkeypatch.setattr(book.config, "DATA_DIR", tmp_path)
    (tmp_path / "positions.json").write_bytes(b'{"sentinel": 1}')
    res = book.sync(tmp_path / "x.xlsx", quiet=True)
    assert res["skipped"] is True and "Refinitiv 미연결" in res["error"]
    assert (tmp_path / "positions.json").read_bytes() == b'{"sentinel": 1}'


def test_book_sync_no_prices_does_not_write(tmp_path, monkeypatch):
    """연결 확인 뒤 세션이 끊겨 가격을 한 건도 못 받으면(KTB10 로컬 이력 제외) 쓰지 않는다."""
    monkeypatch.setattr(refinitiv_src, "available", lambda force=False: True)
    monkeypatch.setattr(book, "fetch_prices", lambda s: {"KTB10": (101.0, TODAY)})
    monkeypatch.setattr(book, "read_book", lambda p: _book_stub())
    monkeypatch.setattr(book.config, "DATA_DIR", tmp_path)
    (tmp_path / "positions.json").write_bytes(b'{"sentinel": 1}')
    res = book.sync(tmp_path / "x.xlsx", quiet=True)
    assert res["skipped"] is True
    assert (tmp_path / "positions.json").read_bytes() == b'{"sentinel": 1}'


def test_book_sync_writes_when_priced(tmp_path, monkeypatch):
    """가격을 받으면 예전처럼 positions.json 을 쓴다(정상 경로 불변)."""
    monkeypatch.setattr(refinitiv_src, "available", lambda force=False: True)
    monkeypatch.setattr(book, "fetch_prices", lambda s: {"069500.KS": (31000.0, TODAY), "KRW=X": (1350.0, TODAY)})
    monkeypatch.setattr(book, "read_book", lambda p: _book_stub())
    monkeypatch.setattr(book.config, "DATA_DIR", tmp_path)
    res = book.sync(tmp_path / "x.xlsx", quiet=True)
    assert not res.get("skipped")
    doc = json.loads((tmp_path / "positions.json").read_text(encoding="utf-8"))
    assert doc["positions"][0]["pnl_as_of"] == TODAY and doc["positions"][0]["unpriced_legs"] == []


# ── 가격 못 받은 다리 ──
def _two_positions():
    legs = [_leg("E1", "069500", "KRX ETF", "KRW", 30000.0, 1.0),
            _leg("E2", "SPY", "NYSE Arca", "USD", 500.0, 1300.0, qty=1e4)]
    pos = {"E1": _pos("코스피", "equity", "KRW"), "E2": _pos("미국 주식", "equity", "USD")}
    return legs, pos


def test_mark_flags_unpriced_legs():
    """가격 못 받은 다리(USD 다리는 KRW=X 미수신 포함)를 포지션에 적고, 전부 못 받으면 기준일은 오늘이 아니라 None."""
    legs, pos = _two_positions()
    recs, _ = book.mark(legs, pos, {}, GA)
    r = {x["id"]: x for x in recs}
    assert r["E1"]["unpriced_legs"] == ["069500.KS"] and r["E1"]["pnl_as_of"] is None
    assert r["E2"]["unpriced_legs"] == ["KRW=X", "SPY"] and r["E2"]["pnl_as_of"] is None

    legs, pos = _two_positions()
    recs, _ = book.mark(legs, pos, {"069500.KS": (31000.0, TODAY), "SPY": (510.0, TODAY)}, GA)
    r = {x["id"]: x for x in recs}
    assert r["E1"]["unpriced_legs"] == [] and r["E1"]["pnl_as_of"] == TODAY
    assert r["E2"]["unpriced_legs"] == ["KRW=X"]


# ── 환 오버레이 방향 ──
def test_fx_overlay_short_gains_when_krw_strengthens():
    """숏 환 오버레이(원화 롱)는 원화 강세 시나리오에서 이익이어야 한다."""
    legs = [_leg("F9", "USD F", "KRX 파생", "KRW", 1300.0, 1.0, name="미국달러선물 최근월물", side="숏",
                 mult=10000.0, qty=1000.0)]
    pos = {"F9": _pos("원화 롱", "fx", "USD", stop=-0.03, target=0.05)}
    recs, _ = book.mark(legs, pos, {}, GA)
    rec = recs[0]
    assert rec["fx_sign"] == -1 and rec["fx_exposure_bn"] == 130.0
    fx_leg = limits.expand_legs([rec])[0]
    assert limits.leg_stress_loss(fx_leg, {"usdkrw_pct": -4}) == pytest.approx(-5.2)
    # fx_net_bn·fx_sign 없는 기존 JSON 은 예전대로(그로스·롱) 손실
    old = {**rec, "fx_net_bn": None, "fx_sign": None}
    assert limits.leg_stress_loss(limits.expand_legs([old])[0], {"usdkrw_pct": -4}) == pytest.approx(5.2)


_LIM = {
    "total_limit_bn": 300, "asset_limits_bn": {"bond": 80, "equity": 160, "fx": 40},
    "stages": {"warn_pct": 50, "reduce_pct": 75, "halt_pct": 100},
    "stop_loss": {"approach_ratio": 0.7, "default_pct": {"bond": -2, "equity": -10, "mixed": -2}, "fx_open_default_pct": -6},
    "stress_scenarios": [{"id": "S0", "name": "s", "usdkrw_pct": -4, "sizing": True}],
    "deadlines": {"warn_response": "당일", "reduce_days": 3},
}


def test_fx_cross_overlay_keeps_gross_base():
    """달러 롱 + 엔 숏 교차 오버레이: 손절·한도 기준액은 그로스(≈260억) — 순노출(≈0)로 붕괴해
    ±1% 움직임에 FX_STOP_HIT 가 뜨면 안 된다. 스트레스만 순노출(fx_net_bn)로 잡는다."""
    legs = [_leg("F8", "USD F", "KRX 파생", "KRW", 1300.0, 1.0, name="미국달러선물 최근월물", mult=10000.0, qty=1000.0),
            _leg("F8", "JPY F", "KRX 파생", "KRW", 1300.0, 1.0, name="엔선물 최근월물", side="숏", mult=10000.0, qty=1000.0)]
    pos = {"F8": _pos("달러/엔 교차", "fx", "USD", stop=-0.03)}
    # 달러 -1%(1287), 엔 +1%(100엔당 1313 → JPYKRW 13.13) — 두 다리 모두 손실 ≈ -2.6억
    recs, _ = book.mark(legs, pos, {"KRW=X": (1287.0, TODAY), "JPYKRW=X": (13.13, TODAY)}, GA)
    rec = recs[0]
    assert rec["fx_exposure_bn"] == pytest.approx(260.0, abs=0.1)
    assert rec["fx_net_bn"] == pytest.approx(-2.6, abs=0.1) and rec["fx_sign"] == -1
    assert rec["pnl_fx_ytd_bn"] == pytest.approx(-2.6, abs=0.01)
    ev = limits.evaluate(positions_doc={"meta": {"as_of": TODAY}, "positions": [rec]}, limits=_LIM)
    fx = ev["positions"][0]["fx"]
    assert fx["stop_amount_bn"] == pytest.approx(7.8, abs=0.1) and fx["stage"] == "NORMAL"
    assert not any(a.get("code") == "FX_STOP_HIT" for a in ev.get("alerts", []))
    # 스트레스는 순노출 기준: 순숏 2.6억 → 원화 강세 4% 에 소폭 이익
    assert limits.leg_stress_loss(limits.expand_legs([rec])[0], {"usdkrw_pct": -4}) == pytest.approx(-0.104, abs=0.01)


# ── 구성 지문·시험 포트 플래그 ──
def test_positions_key_stable_on_remark(tmp_path, monkeypatch):
    """가격만 다시 평가하면 positions_key 는 그대로, 수량을 바꾸면 달라진다. is_trial 은 명시 플래그."""
    monkeypatch.setattr(book.config, "DATA_DIR", tmp_path)

    def key(prices, qty=1e4):
        legs, pos = _two_positions()
        legs[1]["qty"] = qty
        recs, _ = book.mark(legs, pos, prices, GA)
        meta = json.loads(book.write_positions(recs, tmp_path / "x.xlsx", prices).read_text(encoding="utf-8"))["meta"]
        assert meta["is_trial"] is True
        return meta["positions_key"]

    k1 = key({"069500.KS": (31000.0, TODAY), "SPY": (510.0, TODAY), "KRW=X": (1350.0, TODAY)})
    assert len(k1) == 12
    assert key({"069500.KS": (29000.0, TODAY), "SPY": (480.0, TODAY), "KRW=X": (1320.0, TODAY)}) == k1
    assert key({"069500.KS": (31000.0, TODAY)}, qty=2e4) != k1


def test_book_sync_offline_still_writes_limits(tmp_path, monkeypatch):
    """--limits 는 가격이 필요 없다 — 오프라인이어도 limits.json 은 쓰고, positions.json 은 그대로 둔다."""
    monkeypatch.setattr(refinitiv_src, "available", lambda force=False: False)
    monkeypatch.setattr(book, "fetch_prices", lambda s: pytest.fail("오프라인인데 가격 조회"))
    legs, pos, ga = _book_stub()
    ga["_limits"] = {"전체": 300, "주식": 160, "채권": 80, "환": 40}
    monkeypatch.setattr(book, "read_book", lambda p: (legs, pos, ga))
    monkeypatch.setattr(book.config, "DATA_DIR", tmp_path)
    sample = {"meta": {"loss_basis": "x"}, "stop_loss": {"default_pct": {"bond": -2.0}}}
    (tmp_path / "limits.sample.json").write_text(json.dumps(sample), encoding="utf-8")
    (tmp_path / "positions.json").write_bytes(b'{"sentinel": 1}')
    res = book.sync(tmp_path / "x.xlsx", with_limits=True, quiet=True)
    assert res["skipped"] is True and res["limits_path"]
    assert json.loads((tmp_path / "limits.json").read_text(encoding="utf-8"))["total_limit_bn"] == 300
    assert (tmp_path / "positions.json").read_bytes() == b'{"sentinel": 1}'


# ── limits.json 원자적 쓰기 ──
def test_write_limits_keeps_old_file_when_write_fails(tmp_path, monkeypatch):
    """쓰는 도중 실패해도 기존 limits.json 은 온전 — 반쯤 쓴 파일이 가안으로 조용히 대체되는 사고 방지."""
    monkeypatch.setattr(book.config, "DATA_DIR", tmp_path)
    sample = {"meta": {"loss_basis": "x"}, "stop_loss": {"default_pct": {"bond": -2.0}}}
    (tmp_path / "limits.sample.json").write_text(json.dumps(sample), encoding="utf-8")
    (tmp_path / "limits.json").write_text(json.dumps({"meta": {"version": "old"}}), encoding="utf-8")
    orig = pathlib.Path.write_text

    def torn(self, s, *a, **kw):
        orig(self, s[:10], *a, **kw)
        raise OSError("디스크 오류(시험)")

    monkeypatch.setattr(pathlib.Path, "write_text", torn)
    with pytest.raises(OSError):
        book.write_limits({"_limits": {"전체": 300, "주식": 160, "채권": 80, "환": 40}})
    monkeypatch.setattr(pathlib.Path, "write_text", orig)
    assert json.loads((tmp_path / "limits.json").read_bytes())["meta"]["version"] == "old"
