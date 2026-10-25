# -*- coding: utf-8 -*-
"""점검 보고서 자체검증(checks.compare_checks) — 같은 포지션에 조정안이 여러 건일 때 비중 비교."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import checks  # noqa: E402


def _rep(*adj) -> dict:
    return {"risk_grade": "WATCH",
            "adjustments": [{"position_id": pid, "direction": d, "weight_proposed_pct": w} for pid, d, w in adj]}


def test_identical_multi_adjustments_agree():
    """축소+헤지 확대를 똑같이 낸 두 패스는 일치 — 방향이 다른 제안끼리 비중을 비교하면 안 된다."""
    adj = (("E1", "REDUCE", 4.0), ("E1", "HEDGE_UP", 10.0))
    c = checks.compare_checks(_rep(*adj), _rep(*adj))
    assert c["verdict"] == "AGREE" and c["max_weight_pp"] == 0


def test_multi_adjustment_order_independent():
    a = _rep(("E1", "REDUCE", 4.0), ("E1", "HEDGE_UP", 10.0))
    b = _rep(("E1", "HEDGE_UP", 10.0), ("E1", "REDUCE", 4.0))
    assert checks.compare_checks(a, b)["verdict"] == "AGREE"
    assert checks.compare_checks(b, a)["verdict"] == "AGREE"


def test_same_direction_weight_gap_still_counts():
    """같은 방향 비중 차는 그대로 잡는다 — 헤지 10% vs 13% 는 3%p(부분 불일치), 반대 방향은 즉시 불일치."""
    p = _rep(("E1", "REDUCE", 4.0), ("E1", "HEDGE_UP", 10.0))
    v = _rep(("E1", "HEDGE_UP", 13.0), ("E1", "REDUCE", 4.0))
    c = checks.compare_checks(p, v)
    assert c["max_weight_pp"] == 3.0 and c["verdict"] == "PARTIAL"
    c = checks.compare_checks(p, _rep(("E1", "REDUCE", 4.0), ("E1", "HEDGE_UP", 17.0)))
    assert c["max_weight_pp"] == 7.0 and c["verdict"] == "DISAGREE"
    c = checks.compare_checks(_rep(("E1", "INCREASE", 5.0)), _rep(("E1", "REDUCE", 5.0)))
    assert c["opposite_directions"] == 1 and c["verdict"] == "DISAGREE"
