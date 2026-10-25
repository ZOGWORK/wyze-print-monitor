"""Tests for the print-failure decision logic and model I/O (no model file)."""
import numpy as np
import pytest

from wyze_print_monitor import failure_detect as fd
from wyze_print_monitor.failure_detect import (
    FAILURE,
    OK,
    WARNING,
    Baseline,
    Detection,
    FailureScorer,
    draw_detections,
    postprocess,
    preprocess,
)


def _outputs(boxes, confs):
    """Shape detections like the network does: [(1,N,1,4), (1,N,1)]."""
    b = np.asarray(boxes, np.float32).reshape(1, -1, 1, 4)
    c = np.asarray(confs, np.float32).reshape(1, -1, 1)
    return [b, c]


def test_preprocess_makes_nchw_rgb_floats():
    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[..., 2] = 255  # pure red in BGR
    x = preprocess(frame)
    assert x.shape == (1, 3, 416, 416) and x.dtype == np.float32
    assert x[0, 0].mean() == 1.0 and x[0, 2].mean() == 0.0  # R first


def test_postprocess_thresholds_and_suppresses_overlaps():
    out = _outputs(
        [[0.1, 0.1, 0.5, 0.5], [0.12, 0.1, 0.5, 0.52], [0.6, 0.6, 0.9, 0.9], [0, 0, 1, 1]],
        [0.9, 0.7, 0.3, 0.05],
    )
    dets = postprocess(out)
    # The 2nd box overlaps the 1st (dropped by NMS); the 4th is under threshold.
    assert [round(d.confidence, 2) for d in dets] == [0.9, 0.3]
    assert dets[0].box == pytest.approx((0.1, 0.1, 0.5, 0.5))


def test_postprocess_clips_boxes_and_handles_nothing():
    assert postprocess(_outputs([[0, 0, 1, 1]], [0.01])) == []
    (d,) = postprocess(_outputs([[-0.2, 0.1, 1.3, 0.9]], [0.5]))
    assert d.box[0] == 0.0 and d.box[2] == 1.0


def _feed(scorer, p, n):
    return [scorer.update([Detection(p, (0, 0, 1, 1))] if p else []) for _ in range(n)]


def test_never_alerts_during_the_first_minutes():
    scorer = FailureScorer()
    assert set(_feed(scorer, 3.0, fd.INIT_SAFE_FRAME_NUM - 1)) == {OK}


def test_quiet_print_stays_ok():
    scorer = FailureScorer()
    assert set(_feed(scorer, 0.0, 200)) == {OK}
    assert scorer.risk() < 1 / 3


def test_sudden_failure_warns_then_pauses_only_after_it_holds():
    scorer = FailureScorer()
    _feed(scorer, 0.0, 60)  # 10 minutes of a good print
    levels = _feed(scorer, 2.5, 20)
    first_warning = levels.index(WARNING)
    first_failure = levels.index(FAILURE)
    assert first_warning < first_failure
    # FAILURE (pause) needs PAUSE_CONFIRM_CHECKS failing checks in a row.
    assert levels[first_failure - fd.PAUSE_CONFIRM_CHECKS + 1 : first_failure] == [WARNING] * (
        fd.PAUSE_CONFIRM_CHECKS - 1
    )
    assert scorer.risk() > 2 / 3


def test_one_bad_frame_is_not_enough():
    scorer = FailureScorer()
    _feed(scorer, 0.0, 60)
    assert FAILURE not in _feed(scorer, 3.0, 1) + _feed(scorer, 0.0, 30)


def test_a_long_term_baseline_absorbs_constant_false_positives():
    # A camera that always "sees" 0.5 of failure somewhere: the long-term
    # mean learns it, and only a rise above it counts.
    scorer = FailureScorer(Baseline(rolling_mean_long=0.5, lifetime_frames=5000))
    assert FAILURE not in _feed(scorer, 0.5, 300)


def test_sensitivity_scales_the_signal():
    def first_warning(sensitivity):
        s = FailureScorer(sensitivity=sensitivity)
        _feed(s, 0.0, 60)
        levels = _feed(s, 0.9, 60)
        return levels.index(WARNING) if WARNING in levels else 99

    assert first_warning(1.2) <= first_warning(1.0) <= first_warning(0.8)


def test_reset_session_restarts_the_safe_period():
    scorer = FailureScorer()
    _feed(scorer, 0.0, 60)
    scorer.reset_session()
    assert set(_feed(scorer, 3.0, fd.INIT_SAFE_FRAME_NUM - 1)) == {OK}
    assert scorer.baseline.lifetime_frames == 60 + fd.INIT_SAFE_FRAME_NUM - 1  # kept


def test_baseline_round_trip_and_bad_values():
    b = Baseline(0.25, 1234)
    assert Baseline.from_dict(b.to_dict()) == b
    assert Baseline.from_dict({}) == Baseline()
    assert Baseline.from_dict({"rolling_mean_long": "nan"}) == Baseline()
    assert Baseline.from_dict({"rolling_mean_long": -1}) == Baseline()
    assert Baseline.from_dict(None) == Baseline()


def test_draw_detections_returns_a_marked_copy():
    frame = np.zeros((100, 200, 3), np.uint8)
    out = draw_detections(frame, [Detection(0.9, (0.1, 0.1, 0.5, 0.5)), Detection(0.1, (0.6, 0.6, 0.9, 0.9))])
    assert frame.sum() == 0 and out.sum() > 0
    assert out[70:90, 130:170].sum() == 0  # low-confidence box not drawn


def test_pause_delay_is_held_time():
    assert FailureScorer().pause_after_s == 30
    scorer = FailureScorer(pause_after_s=60)
    _feed(scorer, 0.0, 60)
    levels = _feed(scorer, 2.5, 30)
    f = levels.index(FAILURE)
    assert levels[f - 6 : f] == [WARNING] * 6  # failing at 0..50 s, paused at 60 s
    assert scorer.watched_s - scorer.streak_started_s >= 60


def test_switching_interval_mid_streak_keeps_the_delay_in_time():
    scorer = FailureScorer(pause_after_s=120)
    _feed(scorer, 0.0, 60)
    while scorer.failure_streak < 4:  # failing at 0, 10, 20, 30 s
        assert scorer.update([Detection(2.5, (0, 0, 1, 1))]) != FAILURE
    scorer.interval_s = 30  # slower checks from now on
    levels = _feed(scorer, 2.5, 5)
    assert levels[:2] == [WARNING, WARNING]  # 60 s, 90 s
    assert levels[2] == FAILURE  # 120 s: not the 5-checks-at-30 s = 150 s, nor earlier


def test_quiet_start_is_time_even_if_the_interval_changes():
    # 270 s at 30 s checks, then faster checks: quiet ends at 300 s watched,
    # not after another 30 checks.
    scorer = FailureScorer(interval_s=30)
    _feed(scorer, 0.0, 9)
    scorer.interval_s = 10
    _feed(scorer, 0.0, 3)
    assert scorer.watched_s == pytest.approx(300)
    assert WARNING in _feed(scorer, 3.0, 10)

    # And the other way: 100 s at 10 s, then 30 s checks — still quiet at 280 s.
    slow = FailureScorer(interval_s=10)
    _feed(slow, 0.0, 10)
    slow.interval_s = 30
    assert set(_feed(slow, 3.0, 6)) == {OK}
    assert slow.watched_s == pytest.approx(280)


def test_looks_fine_teaches_the_camera_what_is_normal():
    def first_warning(scorer):
        levels = _feed(scorer, 1.2, 60)
        return levels.index(WARNING) if WARNING in levels else 99

    untaught = FailureScorer()
    _feed(untaught, 0.0, 60)
    plain = first_warning(untaught)

    taught = FailureScorer()
    _feed(taught, 0.0, 60)
    _feed(taught, 1.2, 20)  # it raised a (false) alarm on this...
    before = taught.baseline.rolling_mean_long
    taught.teach_normal()  # ..."Looks fine"
    assert taught.baseline.rolling_mean_long > before
    taught.reset_session()
    _feed(taught, 0.0, 30)
    assert first_warning(taught) > plain  # the same thing alerts later, or not at all


def test_teaching_is_capped():
    scorer = FailureScorer()
    scorer.ewm_mean = 50.0
    for _ in range(10):
        scorer.teach_normal()
    assert scorer.baseline.rolling_mean_long <= fd.TEACH_MAX


def test_teaching_never_lowers_a_high_normal():
    scorer = FailureScorer(Baseline(rolling_mean_long=1.5, lifetime_frames=5000))
    scorer.ewm_mean = 2.0
    scorer.teach_normal()
    assert scorer.baseline.rolling_mean_long == 1.5


def test_watch_area_keeps_only_detections_on_the_bed():
    bed = fd.Detection(0.5, (0.4, 0.4, 0.6, 0.6))
    shelf = fd.Detection(0.6, (0.05, 0.72, 0.12, 0.8))  # off to the side
    assert fd.in_area([bed, shelf], (0.25, 0.2, 0.85, 0.95)) == [bed]
    assert fd.in_area([bed, shelf], None) == [bed, shelf]


def test_clean_area_rejects_junk_and_tiny_boxes():
    assert fd.clean_area([0.8, 0.9, 0.2, 0.1]) == (0.2, 0.1, 0.8, 0.9)
    assert fd.clean_area([-1, 0, 2, 1]) == (0.0, 0.0, 1.0, 1.0)
    assert fd.clean_area([0.5, 0.5, 0.51, 0.9]) is None
    for junk in (None, "abc", [1, 2], {}):
        assert fd.clean_area(junk) is None
