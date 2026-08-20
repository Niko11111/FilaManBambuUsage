"""Tests for what a printer's reports mean.

These are the pure parts of tracker.py: merging the partial reports a Bambu
printer sends, deciding whether a print started or ended, and reading a job out
of the merged state. No MQTT, no database, no printer, and no optional
dependency either.

The payload shapes follow docs/03_Bambu_Data_Sources.md.
"""

from __future__ import annotations

import unittest

from bambu_usage.tracker import (
    PRINT_TYPE_CLOUD,
    PRINT_TYPE_LOCAL,
    STATE_FAILED,
    STATE_FINISH,
    STATE_IDLE,
    STATE_PAUSE,
    STATE_PREPARE,
    STATE_RUNNING,
    TRANSITION_ENDED,
    TRANSITION_STARTED,
    completed_fraction,
    describe_job,
    detect_transition,
    merge_report,
    progress_of,
)


def report(**fields):
    """One MQTT report, everything under the print section as the printer does."""
    return {"print": fields}


class MergeReportTest(unittest.TestCase):
    """A printer sends what changed, not what is."""

    def test_a_partial_update_keeps_the_rest(self):
        state = merge_report(
            report(gcode_state=STATE_RUNNING, subtask_name="cube.3mf"),
            report(mc_percent=42),
        )
        self.assertEqual(state["print"]["subtask_name"], "cube.3mf")
        self.assertEqual(state["print"]["mc_percent"], 42)

    def test_a_changed_field_wins(self):
        state = merge_report(report(gcode_state=STATE_RUNNING), report(gcode_state=STATE_FINISH))
        self.assertEqual(state["print"]["gcode_state"], STATE_FINISH)

    def test_a_new_section_is_added(self):
        state = merge_report(report(gcode_state=STATE_IDLE), {"info": {"command": "get_version"}})
        self.assertIn("info", state)
        self.assertEqual(state["print"]["gcode_state"], STATE_IDLE)

    def test_a_non_dict_replaces_instead_of_merging(self):
        # Firmware is free to change a shape. Merging a list into a dict would
        # raise inside a callback, where nobody would see it.
        state = merge_report({"print": {"a": 1}}, {"print": ["not", "a", "dict"]})
        self.assertEqual(state["print"], ["not", "a", "dict"])

    def test_the_previous_state_is_left_alone(self):
        # detect_transition compares before against after, so merging in place
        # would quietly make every transition invisible.
        previous = report(gcode_state=STATE_RUNNING)
        merge_report(previous, report(gcode_state=STATE_FINISH))
        self.assertEqual(previous["print"]["gcode_state"], STATE_RUNNING)


class DetectTransitionTest(unittest.TestCase):
    def test_idle_to_running_starts_a_print(self):
        found = detect_transition(report(gcode_state=STATE_IDLE), report(gcode_state=STATE_RUNNING))
        self.assertEqual(found.kind, TRANSITION_STARTED)

    def test_prepare_to_running_is_the_same_print(self):
        self.assertIsNone(
            detect_transition(report(gcode_state=STATE_PREPARE), report(gcode_state=STATE_RUNNING))
        )

    def test_nothing_happens_while_it_runs(self):
        self.assertIsNone(
            detect_transition(
                report(gcode_state=STATE_RUNNING, mc_percent=10),
                report(gcode_state=STATE_RUNNING, mc_percent=11),
            )
        )

    def test_running_to_finish_ends_it(self):
        found = detect_transition(report(gcode_state=STATE_RUNNING), report(gcode_state=STATE_FINISH))
        self.assertEqual(found.kind, TRANSITION_ENDED)
        self.assertEqual(found.gcode_state, STATE_FINISH)

    def test_running_to_failed_ends_it_too(self):
        found = detect_transition(report(gcode_state=STATE_RUNNING), report(gcode_state=STATE_FAILED))
        self.assertEqual(found.gcode_state, STATE_FAILED)

    def test_a_paused_print_can_still_end(self):
        found = detect_transition(report(gcode_state=STATE_PAUSE), report(gcode_state=STATE_FINISH))
        self.assertEqual(found.kind, TRANSITION_ENDED)

    def test_the_first_report_of_a_running_printer_is_a_start(self):
        # The plugin attached in the middle of a print. Recording it is the
        # caller's business, but it has to be noticed at all.
        found = detect_transition({}, report(gcode_state=STATE_RUNNING))
        self.assertEqual(found.kind, TRANSITION_STARTED)

    def test_a_new_job_without_leaving_running(self):
        # One print straight into the next. Only the subtask id gives it away,
        # and missing it would book the second print onto the first.
        found = detect_transition(
            report(gcode_state=STATE_RUNNING, subtask_id="1"),
            report(gcode_state=STATE_RUNNING, subtask_id="2"),
        )
        self.assertEqual(found.kind, TRANSITION_STARTED)

    def test_finishing_and_going_idle_is_not_an_event(self):
        self.assertIsNone(
            detect_transition(report(gcode_state=STATE_FINISH), report(gcode_state=STATE_IDLE))
        )

    def test_a_report_without_a_state_says_nothing(self):
        self.assertIsNone(detect_transition({}, report(mc_percent=5)))


class DescribeJobTest(unittest.TestCase):
    def test_a_cloud_print_carries_its_mapping(self):
        job = describe_job(
            report(
                command="project_file",
                url="https://example.invalid/job.3mf",
                subtask_id="42",
                subtask_name="cube.3mf",
                print_type="cloud",
                ams_mapping=[1, 0],
            )
        )
        self.assertEqual(job.subtask_id, "42")
        self.assertEqual(job.file_name, "cube.3mf")
        self.assertEqual(job.print_type, PRINT_TYPE_CLOUD)
        self.assertEqual(job.ams_mapping, [1, 0])
        self.assertEqual(job.url, "https://example.invalid/job.3mf")

    def test_a_local_print_names_a_file_on_the_printer(self):
        job = describe_job(
            report(gcode_file="/data/Metadata/plate_1.gcode.3mf", print_type="local")
        )
        self.assertEqual(job.print_type, PRINT_TYPE_LOCAL)
        self.assertEqual(job.remote_path, "/data/Metadata/plate_1.gcode.3mf")
        # No mapping at all, which is why local prints land with an open
        # assignment instead of a guessed one.
        self.assertEqual(job.ams_mapping, [])

    def test_the_name_falls_back_to_the_file_on_the_printer(self):
        job = describe_job(report(gcode_file="/cache/bracket.3mf"))
        self.assertEqual(job.file_name, "bracket.3mf")

    def test_a_print_with_no_name_at_all_still_gets_one(self):
        # It has to be recordable. A print that vanishes without trace is worse
        # than one without numbers.
        self.assertEqual(describe_job(report(gcode_state=STATE_RUNNING)).file_name, "unknown")

    def test_the_type_is_derived_when_the_printer_does_not_say(self):
        self.assertEqual(describe_job(report(url="https://x.invalid/a.3mf")).print_type, PRINT_TYPE_CLOUD)
        self.assertEqual(describe_job(report(gcode_file="a.3mf")).print_type, PRINT_TYPE_LOCAL)

    def test_a_mapping_that_is_not_a_list_is_dropped(self):
        self.assertEqual(describe_job(report(ams_mapping="0,1")).ams_mapping, [])

    def test_an_empty_subtask_id_counts_as_none(self):
        self.assertIsNone(describe_job(report(subtask_id="")).subtask_id)


class ProgressTest(unittest.TestCase):
    def test_a_percentage(self):
        self.assertEqual(progress_of(report(mc_percent=42)), 42)

    def test_a_percentage_written_as_text(self):
        self.assertEqual(progress_of(report(mc_percent="42")), 42)

    def test_no_percentage(self):
        self.assertIsNone(progress_of(report(gcode_state=STATE_RUNNING)))
        self.assertIsNone(progress_of(report(mc_percent="almost")))


class CompletedFractionTest(unittest.TestCase):
    """How far a print got, for booking what a stopped print actually used."""

    def test_layers_come_first(self):
        # A layer is a better proxy for material than time is, and mc_percent
        # on a Bambu is progress in time.
        state = report(layer_num=50, total_layer_num=200, mc_percent=40)
        self.assertEqual(completed_fraction(state), 0.25)

    def test_the_percentage_stands_in_when_layers_are_missing(self):
        self.assertEqual(completed_fraction(report(mc_percent=40)), 0.4)

    def test_no_progress_at_all_is_not_zero_but_unknown(self):
        # Zero would book nothing quietly; None says so, and the caller decides.
        self.assertIsNone(completed_fraction(report(gcode_state=STATE_RUNNING)))

    def test_a_print_without_layers_yet(self):
        self.assertIsNone(completed_fraction(report(layer_num=0, total_layer_num=0)))

    def test_more_layers_than_the_total(self):
        self.assertEqual(completed_fraction(report(layer_num=205, total_layer_num=200)), 1.0)

    def test_numbers_written_as_text(self):
        self.assertEqual(completed_fraction(report(layer_num="1", total_layer_num="4")), 0.25)


if __name__ == "__main__":
    unittest.main()
