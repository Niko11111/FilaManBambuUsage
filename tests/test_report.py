"""Tests for what a printer's reports mean.

report.py is the half of the listener that decides things: merging the partial
reports a Bambu printer sends, telling whether a print started or ended, and
reading a job out of the merged state. No MQTT, no database, no printer, and no
optional dependency either.

The payload shapes follow docs/03_Bambu_Data_Sources.md.
"""

from __future__ import annotations

import unittest

from bambu_usage.report import (
    EXTERNAL_AMS_ID,
    active_tray,
    EXTERNAL_TRAY_ID,
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
    layer_of,
    merge_report,
    progress_of,
    remaining_minutes_of,
    error_code,
    total_layers_of,
    tray_tags,
)
from bambu_usage.rules import EXTERNAL_SLOT_INDEX, slot_index, tray_to_slot_index


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


class LiveFiguresTest(unittest.TestCase):
    """What the card of a running print shows while it runs."""

    def test_layers(self):
        state = report(layer_num=58, total_layer_num=73)
        self.assertEqual(layer_of(state), 58)
        self.assertEqual(total_layers_of(state), 73)

    def test_layers_written_as_text(self):
        state = report(layer_num="58", total_layer_num="73")
        self.assertEqual(layer_of(state), 58)
        self.assertEqual(total_layers_of(state), 73)

    def test_no_layers_reported(self):
        self.assertIsNone(layer_of(report(gcode_state=STATE_RUNNING)))
        self.assertIsNone(total_layers_of(report(gcode_state=STATE_RUNNING)))

    def test_remaining_time(self):
        self.assertEqual(remaining_minutes_of(report(mc_remaining_time=26)), 26)

    def test_no_remaining_time(self):
        self.assertIsNone(remaining_minutes_of(report(gcode_state=STATE_RUNNING)))
        self.assertIsNone(remaining_minutes_of(report(mc_remaining_time="soon")))


class ActiveTrayTest(unittest.TestCase):
    """Which tray the printer is drawing from."""

    def test_reads_the_loaded_tray(self):
        self.assertEqual(active_tray({"print": {"ams": {"tray_now": "3"}}}), 3)

    def test_the_external_holder_comes_back_negative(self):
        # 255 worked out as a global tray number would be tray 3 of AMS 63.
        # rules.tray_to_slot_index wants a negative number for the holder.
        tray = active_tray({"print": {"ams": {"tray_now": "255"}}})

        self.assertLess(tray, 0)
        self.assertEqual(tray_to_slot_index(tray), EXTERNAL_SLOT_INDEX)

    def test_silence_is_not_tray_zero(self):
        for state in ({}, {"print": {}}, {"print": {"ams": {}}}, {"print": {"ams": "x"}}):
            with self.subTest(state=state):
                self.assertIsNone(active_tray(state))


class ErrorCodeTest(unittest.TestCase):
    """What the printer said about why a print ended."""

    def test_a_code_is_read(self):
        self.assertEqual(error_code({"print": {"print_error": 50348044}}), 50348044)

    def test_the_other_field_counts_too(self):
        self.assertEqual(error_code({"print": {"mc_print_error_code": 117440512}}), 117440512)

    def test_zero_is_no_fault(self):
        # A print somebody stopped reports zero, and zero must not read as a
        # fault, or every cancelled print would look like a broken printer.
        self.assertIsNone(error_code({"print": {"print_error": 0}}))

    def test_nothing_reported_is_no_fault(self):
        self.assertIsNone(error_code({"print": {}}))
        self.assertIsNone(error_code({}))


class TrayTagsTest(unittest.TestCase):
    """What sits in which tray, read out of the AMS section of a report."""

    def state(self, **print_section):
        return {"print": print_section}

    def ams(self, *trays, unit="0"):
        return self.state(ams={"ams": [{"id": unit, "tray": list(trays)}]})

    def test_reads_a_tag_out_of_a_tray(self):
        tags = tray_tags(self.ams({"id": "1", "tray_uuid": "ABC123"}))

        self.assertEqual([(t.ams_id, t.tray_id, t.uuid) for t in tags], [(0, 1, "ABC123")])

    def test_an_empty_tray_carries_no_tag(self):
        # Every empty chamber reports the same string of zeros. Looking it up
        # would hang all of them on whichever spool happens to carry it.
        tags = tray_tags(self.ams({"id": "0", "tray_uuid": "0" * 32}))

        self.assertEqual(tags, [])

    def test_the_external_holder_counts_too(self):
        tags = tray_tags(self.state(vt_tray={"id": "254", "tray_uuid": "EXT"}))

        self.assertEqual(len(tags), 1)
        self.assertEqual(slot_index(tags[0].ams_id, tags[0].tray_id), EXTERNAL_SLOT_INDEX)

    def test_the_external_ids_agree_with_the_rules(self):
        # report.py may not import rules, so the pair is written down twice.
        # This is what keeps the two copies from drifting apart.
        self.assertEqual(slot_index(EXTERNAL_AMS_ID, EXTERNAL_TRAY_ID), EXTERNAL_SLOT_INDEX)

    def test_a_second_unit_keeps_its_own_number(self):
        state = {"print": {"ams": {"ams": [
            {"id": "0", "tray": [{"id": "3", "tray_uuid": "FIRST"}]},
            {"id": "1", "tray": [{"id": "0", "tray_uuid": "SECOND"}]},
        ]}}}

        tags = tray_tags(state)

        self.assertEqual(
            [slot_index(t.ams_id, t.tray_id) for t in tags], ["0-3", "1-0"]
        )

    def test_nothing_in_the_report_is_trusted(self):
        # The report comes off a printer and a firmware update may rename or
        # drop any of it. None of these may raise.
        for state in (
            {},
            {"print": "not a section"},
            {"print": {"ams": "not a dict"}},
            {"print": {"ams": {"ams": "not a list"}}},
            {"print": {"ams": {"ams": [{"tray": [{"id": "0", "tray_uuid": "X"}]}]}}},
            {"print": {"ams": {"ams": [{"id": "0", "tray": "not a list"}]}}},
            {"print": {"ams": {"ams": [{"id": "0", "tray": [{"tray_uuid": "X"}]}]}}},
            {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_uuid": 42}]}]}}},
            {"print": {"vt_tray": "not a dict"}},
        ):
            with self.subTest(state=state):
                self.assertEqual(tray_tags(state), [])


if __name__ == "__main__":
    unittest.main()
