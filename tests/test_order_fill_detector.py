from __future__ import annotations

from app.services.order_fill_detector import OrderFillDetector


def test_first_observation_is_a_silent_baseline() -> None:
    detector = OrderFillDetector()

    assert detector.observe(["O1", "O2"]) is False


def test_new_id_after_baseline_fires_once() -> None:
    detector = OrderFillDetector()
    detector.observe(["O1"])

    assert detector.observe(["O1", "O2"]) is True
    assert detector.observe(["O1", "O2"]) is False


def test_multiple_new_ids_in_one_update_still_fire_once() -> None:
    detector = OrderFillDetector()
    detector.observe([])

    assert detector.observe(["O1", "O2", "O3"]) is True
    assert detector.observe(["O1", "O2", "O3"]) is False


def test_empty_baseline_then_empty_observation_does_not_fire() -> None:
    detector = OrderFillDetector()
    detector.observe([])

    assert detector.observe([]) is False
