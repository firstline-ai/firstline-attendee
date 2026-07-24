import datetime
from contextlib import ExitStack
from unittest.mock import Mock, patch

from django.db import OperationalError
from django.test import SimpleTestCase
from django.utils import timezone

from bots.bot_controller.bot_controller import BotController
from bots.bot_controller.bot_resource_snapshot_taker import BotResourceSnapshotTaker


class BotResourceSnapshotBestEffortTests(SimpleTestCase):
    def _due_snapshot_taker(self):
        bot = Mock()
        bot.object_id = "bot_resource_test"
        bot.save_resource_snapshots.return_value = True

        with patch("bots.bot_controller.bot_resource_snapshot_taker.threading.Thread"):
            snapshot_taker = BotResourceSnapshotTaker(bot)

        now = timezone.now()
        snapshot_taker._last_snapshot_time = now - datetime.timedelta(seconds=61)
        snapshot_taker._first_cpu_usage_millicores = 1_000
        snapshot_taker._first_cpu_usage_sample_time = now - datetime.timedelta(seconds=30)
        snapshot_taker._first_network_stats = {
            "rx_bytes": 1_000,
            "rx_packets": 10,
            "rx_dropped": 0,
            "rx_errors": 0,
            "tx_bytes": 2_000,
            "tx_packets": 20,
            "tx_dropped": 0,
            "tx_errors": 0,
        }
        snapshot_taker._first_network_sample_time = now - datetime.timedelta(seconds=30)
        return snapshot_taker

    def _snapshot_dependencies(self):
        return (
            patch(
                "bots.bot_controller.bot_resource_snapshot_taker.get_cpu_usage_millicores",
                return_value=13_000,
            ),
            patch(
                "bots.bot_controller.bot_resource_snapshot_taker.container_memory_mib",
                return_value=512,
            ),
            patch(
                "bots.bot_controller.bot_resource_snapshot_taker.get_network_interface_stats",
                return_value={
                    "rx_bytes": 2_000,
                    "rx_packets": 20,
                    "rx_dropped": 0,
                    "rx_errors": 0,
                    "tx_bytes": 4_000,
                    "tx_packets": 40,
                    "tx_dropped": 0,
                    "tx_errors": 0,
                },
            ),
            patch(
                "bots.bot_controller.bot_resource_snapshot_taker.get_process_memory_list",
                return_value=[],
            ),
            patch(
                "bots.bot_controller.bot_resource_snapshot_taker.get_db_connection_count",
                return_value=1,
            ),
            patch(
                "bots.bot_controller.bot_resource_snapshot_taker.get_redis_connection_count",
                return_value=2,
            ),
        )

    def test_database_write_failure_is_best_effort(self):
        snapshot_taker = self._due_snapshot_taker()

        with ExitStack() as stack:
            for dependency_patch in self._snapshot_dependencies():
                stack.enter_context(dependency_patch)
            create_snapshot = stack.enter_context(
                patch(
                    "bots.bot_controller.bot_resource_snapshot_taker.BotResourceSnapshot.objects.create",
                    side_effect=OperationalError("database unavailable"),
                )
            )
            logs = stack.enter_context(
                self.assertLogs(
                    "bots.bot_controller.bot_resource_snapshot_taker",
                    level="ERROR",
                )
            )
            snapshot_taker.save_snapshot_if_needed()

        create_snapshot.assert_called_once()
        self.assertIn(
            "Failed to save resource snapshot for bot bot_resource_test",
            logs.output[0],
        )

    def test_database_write_failure_does_not_stop_bot_main_loop(self):
        snapshot_taker = self._due_snapshot_taker()

        controller = BotController.__new__(BotController)
        controller.first_timeout_call = False
        controller.set_bot_heartbeat = Mock()
        controller.per_participant_non_streaming_audio_input_manager = None
        controller.audio_chunk_uploader = None
        controller.per_participant_streaming_audio_input_manager = None
        controller.closed_caption_manager = None
        controller.adapter = Mock()
        controller.audio_output_manager = Mock()
        controller.video_output_manager = Mock()
        controller.join_if_staged_and_time_to_join = Mock()
        controller.bot_resource_snapshot_taker = snapshot_taker
        controller.handle_exception_in_timeout_callback = Mock()

        with ExitStack() as stack:
            for dependency_patch in self._snapshot_dependencies():
                stack.enter_context(dependency_patch)
            create_snapshot = stack.enter_context(
                patch(
                    "bots.bot_controller.bot_resource_snapshot_taker.BotResourceSnapshot.objects.create",
                    side_effect=OperationalError("database unavailable"),
                )
            )
            stack.enter_context(
                self.assertLogs(
                    "bots.bot_controller.bot_resource_snapshot_taker",
                    level="ERROR",
                )
            )
            should_continue = controller.on_main_loop_timeout()

        self.assertTrue(should_continue)
        create_snapshot.assert_called_once()
        controller.handle_exception_in_timeout_callback.assert_not_called()
        controller.join_if_staged_and_time_to_join.assert_called_once_with()
