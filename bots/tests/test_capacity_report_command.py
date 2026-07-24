import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

SNAPSHOT_ROWS = [
    (
        101,
        {
            "ram_usage_megabytes": 100,
            "cpu_usage_millicores": 200,
            "db_connection_count": 2,
            "redis_connection_count": 10,
        },
    ),
    (
        101,
        {
            "ram_usage_megabytes": 200,
            "cpu_usage_millicores": 400,
            "db_connection_count": 4,
            "redis_connection_count": 20,
        },
    ),
    (
        202,
        {
            "ram_usage_megabytes": 300,
            "cpu_usage_millicores": 600,
            "db_connection_count": 6,
            "redis_connection_count": 30,
        },
    ),
    (
        202,
        {
            "ram_usage_megabytes": 400,
            "cpu_usage_millicores": 800,
            "db_connection_count": 8,
            "redis_connection_count": 40,
        },
    ),
]


class CapacityReportCommandTests(SimpleTestCase):
    @patch(
        "bots.management.commands.capacity_report.iter_snapshot_rows",
        return_value=iter(SNAPSHOT_ROWS),
    )
    def test_json_report_has_deterministic_percentiles(self, iter_rows):
        output = StringIO()

        call_command("capacity_report", "--days=14", "--json", stdout=output)

        report = json.loads(output.getvalue())
        self.assertEqual(report["window_days"], 14)
        self.assertEqual(report["snapshot_count"], 4)
        self.assertEqual(report["sampled_bot_count"], 2)
        self.assertEqual(
            report["metrics"]["ram_usage_megabytes"],
            {"sample_count": 4, "p50": 200, "p95": 400, "max": 400},
        )
        self.assertEqual(
            report["metrics"]["cpu_usage_millicores"],
            {"sample_count": 4, "p50": 400, "p95": 800, "max": 800},
        )
        self.assertEqual(
            report["metrics"]["db_connection_count"],
            {"sample_count": 4, "p95": 8, "max": 8},
        )
        self.assertEqual(
            report["metrics"]["redis_connection_count"],
            {"sample_count": 4, "p95": 40, "max": 40},
        )
        iter_rows.assert_called_once()

    @patch(
        "bots.management.commands.capacity_report.iter_snapshot_rows",
        return_value=iter(SNAPSHOT_ROWS),
    )
    def test_human_report_is_compact_and_contains_no_bot_ids(self, _iter_rows):
        output = StringIO()

        call_command("capacity_report", stdout=output)

        report = output.getvalue()
        self.assertIn("Attendee capacity report (last 30 day(s))", report)
        self.assertIn("snapshots=4", report)
        self.assertIn("sampled_bots=2", report)
        self.assertIn(
            "ram_usage_megabytes: samples=4 p50=200 p95=400 max=400",
            report,
        )
        self.assertIn("db_connection_count: samples=4 p95=8 max=8", report)
        self.assertNotIn("101", report)
        self.assertNotIn("202", report)

    @patch(
        "bots.management.commands.capacity_report.iter_snapshot_rows",
        return_value=iter(
            [
                (101, {}),
                (202, {"ram_usage_megabytes": None}),
                (202, {"ram_usage_megabytes": True}),
            ]
        ),
    )
    def test_missing_and_boolean_metric_values_are_not_samples(self, _iter_rows):
        output = StringIO()

        call_command("capacity_report", "--json", stdout=output)

        report = json.loads(output.getvalue())
        self.assertEqual(
            report["metrics"]["ram_usage_megabytes"],
            {"sample_count": 0, "p50": None, "p95": None, "max": None},
        )
        self.assertEqual(report["snapshot_count"], 3)
        self.assertEqual(report["sampled_bot_count"], 2)

    def test_non_positive_window_is_rejected(self):
        with self.assertRaisesMessage(
            CommandError,
            "--days must be greater than zero.",
        ):
            call_command("capacity_report", "--days=0")
