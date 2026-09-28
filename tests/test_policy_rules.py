"""Enforced numbers come from policy front matter (one source of truth)."""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from opspilot.config import POLICY_DIR, _load_rules, policy_rules


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "policies"
    shutil.copytree(POLICY_DIR, target)
    return target


def test_current_rules() -> None:
    r = policy_rules()
    assert r.auto_refund_limit == 200
    assert r.return_windows == {"electronics": 14, "apparel": 30, "home": 30, "grocery": None}
    assert r.goodwill_limits == {"standard": 0, "gold": 10, "platinum": 15}
    assert (r.max_coupon_percent, r.coupon_validity_days) == (15, 90)
    assert (r.promise_days, r.delay_threshold_days, r.delay_coupon_percent) == (7, 10, 10)
    assert r.sources["return_windows"] == "return_policy_v2", "superseded v1 is ignored"


def test_until_v2_takes_effect_v1_windows_apply(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    f = d / "return_policy_v2.md"
    f.write_text(
        f.read_text(encoding="utf-8").replace(
            "effective_date: 2026-03-01", "effective_date: 2026-12-01"
        ),
        encoding="utf-8",
    )
    r = _load_rules(str(d), date(2026, 9, 27), (3.0,))
    assert (
        r.return_windows["electronics"] == 30 and r.sources["return_windows"] == "return_policy_v1"
    )


def test_missing_rule_is_a_clear_error(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    (d / "refund_policy.md").unlink()
    with pytest.raises(ValueError, match="auto_refund_limit"):
        _load_rules(str(d), date(2026, 9, 27), (4.0,))


def _text(name: str) -> str:
    return " ".join((POLICY_DIR / name).read_text(encoding="utf-8").split("---", 2)[2].split())


def test_front_matter_matches_policy_prose() -> None:
    r = policy_rules()
    v2 = _text("return_policy_v2.md")
    assert f"within {r.return_windows['electronics']} days of delivery" in v2
    assert f"within {r.return_windows['apparel']} days of delivery" in v2
    assert "non-returnable" in v2 and r.return_windows["grocery"] is None
    assert f"${r.auto_refund_limit} or less" in _text("refund_policy.md")
    loyalty = _text("loyalty_program.md")
    assert (
        f"up to a {r.goodwill_limits['gold']}%" in loyalty
        or f"up to {r.goodwill_limits['gold']}%" in loyalty
    )
    assert f"up to {r.goodwill_limits['platinum']}%" in loyalty
    assert f"above {r.max_coupon_percent}%" in loyalty
    assert f"valid for {r.coupon_validity_days} days" in loyalty
    shipping = _text("shipping_policy.md")
    assert f"{r.promise_days} calendar days" in shipping
    assert re.search(rf"more than {r.delay_threshold_days} days past", shipping)
    assert f"{r.delay_coupon_percent}% coupon" in shipping


def test_changing_front_matter_changes_enforcement(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    f = d / "refund_policy.md"
    f.write_text(
        f.read_text(encoding="utf-8").replace("auto_refund_limit: 200", "auto_refund_limit: 500"),
        encoding="utf-8",
    )
    assert _load_rules(str(d), date(2026, 9, 27), (1.0,)).auto_refund_limit == 500


def test_duplicate_rule_in_two_current_docs_is_an_error(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    f = d / "shipping_policy.md"
    f.write_text(
        f.read_text(encoding="utf-8").replace(
            "  promise_days: 7", "  promise_days: 7\n  auto_refund_limit: 300"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="auto_refund_limit"):
        _load_rules(str(d), date(2026, 9, 27), (2.0,))
