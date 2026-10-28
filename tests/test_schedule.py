"""Tests for scheduled prints: queue rules and Studio matching (pure)."""
from datetime import datetime, timedelta

from wyze_print_monitor import schedule
from wyze_print_monitor.schedule import (
    CANCELLED,
    FAILED,
    MISSED_AFTER,
    SCHEDULED,
    SENDING,
    STARTED,
    clashes,
    default_time,
    describe_in,
    describe_when,
    due,
    from_dicts,
    history,
    new_job,
    prune,
    to_dicts,
    upcoming,
)
from wyze_print_monitor.studio_win32 import pick_print_button, project_name, split_choose_printer

NOW = datetime(2026, 10, 28, 14, 30)

# eufyMake Studio maximized at 1920x1080 (window rect as Windows reports it).
STUDIO = (-9, -9, 1938, 1038)
EXPORT = (210258, (1426, 970, 237, 36))
PRINT = (275806, (1673, 970, 237, 36))


def test_jobs_round_trip_and_bad_entries_are_dropped():
    job = new_job(NOW + timedelta(hours=2), 2, "  Benchy ")
    assert job.label == "Benchy" and job.printer_row == 2 and job.state == SCHEDULED
    data = to_dicts([job]) + [{"id": "x", "at": "not a time"}, "junk", {"nope": 1}]
    assert from_dicts(data) == [job]
    assert from_dicts(None) == []


def test_due_starts_earliest_on_time_job_and_reports_late_ones():
    late = new_job(NOW - MISSED_AFTER - timedelta(minutes=1), 1)
    first = new_job(NOW - timedelta(minutes=1), 1)
    second = new_job(NOW, 2)
    later = new_job(NOW + timedelta(minutes=5), 1)
    start, missed = due([later, second, late, first], NOW)
    assert start is first
    assert missed == [late]
    assert due([later], NOW) == (None, [])


def test_jobs_being_sent_or_done_are_never_started_again():
    sending = new_job(NOW, 1)
    sending.state = SENDING
    done = new_job(NOW, 1)
    done.finish(STARTED, NOW)
    assert due([sending, done], NOW) == (None, [])
    assert upcoming([sending, done]) == [sending]


def test_history_is_newest_first_and_prune_keeps_pending():
    jobs = []
    for i in range(20):
        job = new_job(NOW + timedelta(minutes=i), 1)
        job.finish(FAILED if i % 2 else CANCELLED, NOW + timedelta(minutes=i))
        jobs.append(job)
    pending = new_job(NOW + timedelta(days=1), 1)
    jobs.append(pending)
    assert history(jobs, 3) == [jobs[19], jobs[18], jobs[17]]
    kept = prune(jobs, keep=5)
    assert pending in kept and len(kept) == 6


def test_clashes_only_same_printer_same_minute():
    job = new_job(NOW, 1)
    assert clashes([job], NOW + timedelta(seconds=30), 1) is job
    assert clashes([job], NOW, 2) is None
    assert clashes([job], NOW + timedelta(minutes=2), 1) is None
    assert clashes([job], NOW, 1, ignore=job.id) is None


def test_describing_times():
    assert describe_when(NOW.replace(hour=15, minute=0), NOW) == "Today 3:00 PM"
    assert describe_when(NOW + timedelta(days=1), NOW) == "Tomorrow 2:30 PM"
    assert describe_when(datetime(2026, 11, 2, 9, 5), NOW) == "Mon Nov 2, 9:05 AM"
    assert describe_in(NOW + timedelta(minutes=5), NOW) == "in 5 min"
    assert describe_in(NOW + timedelta(hours=2, minutes=10), NOW) == "in 2 h 10 min"
    assert describe_in(NOW + timedelta(hours=3), NOW) == "in 3 h"
    assert describe_in(NOW + timedelta(days=2), NOW) == "in 2 days"
    assert describe_in(NOW - timedelta(minutes=1), NOW) == "now"


def test_default_time_is_a_full_hour_at_least_15_min_away():
    assert default_time(NOW) == datetime(2026, 10, 28, 15, 0)
    assert default_time(datetime(2026, 10, 28, 14, 50)) == datetime(2026, 10, 28, 16, 0)
    assert default_time(datetime(2026, 10, 28, 13, 45)) == datetime(2026, 10, 28, 14, 0)


def test_print_button_is_right_of_the_bottom_pair():
    assert pick_print_button([[EXPORT, PRINT]], STUDIO) == PRINT[0]
    assert pick_print_button([[PRINT, EXPORT]], STUDIO) == PRINT[0]


def test_print_button_needs_one_unambiguous_pair():
    # Printer list rows elsewhere in the window: same size, but stacked.
    rows = [(1, (15, 167, 256, 48)), (2, (15, 215, 256, 48))]
    # The Device tab's two top-right icons: too small and too high.
    icons = [(3, (1804, 54, 48, 42)), (4, (1862, 54, 48, 42))]
    assert pick_print_button([rows, icons], STUDIO) is None
    assert pick_print_button([rows, icons, [EXPORT, PRINT]], STUDIO) == PRINT[0]
    other = [(5, (1426, 900, 237, 36)), (6, (1673, 900, 237, 36))]
    assert pick_print_button([[EXPORT, PRINT], other], STUDIO) is None
    assert pick_print_button([[EXPORT, PRINT, (7, (1500, 970, 20, 20))]], STUDIO) is None


def test_choose_printer_dialog_parts():
    # Mapped from Studio's "Choose a Printer" dialog.
    rows = [(11668844, (711, 418, 480, 91)), (525426, (711, 327, 480, 91))]
    buttons = [(657646, (951, 622, 204, 38)), (344170, (735, 622, 204, 38))]
    parts = split_choose_printer(409742, rows, buttons)
    assert parts.rows == [525426, 11668844]
    assert parts.cancel == 344170 and parts.print == 657646
    assert split_choose_printer(1, [], buttons) is None
    assert split_choose_printer(1, rows, buttons[:1]) is None


def test_project_name_from_studio_title():
    assert project_name("*benchy - eufyMake Studio 3D Based on PrusaSlicer") == "benchy"
    assert project_name("*Untitled - eufyMake Studio 3D Based on PrusaSlicer") == ""
    assert project_name("eufyMake Studio") == ""


def test_scheduled_print_label_falls_back_to_printer():
    job = schedule.new_job(NOW, 3)
    assert job.label == "" and job.pending


def test_queue_goes_below_the_printer_rows():
    from wyze_print_monitor.ui.schedule_ui import queue_rect

    rows = [(0, 50, 256, 48), (0, 98, 256, 48)]
    x, y, w, h = queue_rect((0, 0, 268, 800), rows)
    assert y >= 146 + 16 and y >= 0.4 * 800
    assert x + w <= 268 and y + h <= 800
    assert queue_rect((0, 0, 268, 300), rows) is None  # no room left
