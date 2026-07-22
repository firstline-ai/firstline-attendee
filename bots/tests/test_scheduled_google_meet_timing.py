from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase

from bots.bot_controller.bot_controller import BotController
from bots.google_meet_bot_adapter.google_meet_bot_adapter import GoogleMeetBotAdapter
from bots.models import MeetingTypes


class ScheduledGoogleMeetTimingTests(SimpleTestCase):
    def _automatic_leave_configuration(
        self,
        *,
        meeting_type=MeetingTypes.GOOGLE_MEET,
        join_at=object(),
        only_participant_timeout=60,
    ):
        bot = SimpleNamespace(
            join_at=join_at,
            automatic_leave_settings=lambda: {
                "only_participant_in_meeting_timeout_seconds": only_participant_timeout,
            },
        )
        controller = BotController.__new__(BotController)
        controller.bot_in_db = bot
        controller.get_meeting_type = Mock(return_value=meeting_type)
        return controller.get_automatic_leave_configuration()

    def test_staged_google_meet_starts_joining_75_seconds_early(self):
        adapter = GoogleMeetBotAdapter.__new__(GoogleMeetBotAdapter)

        self.assertEqual(adapter.get_staged_bot_join_delay_seconds(), 75)

    def test_scheduled_google_meet_waits_at_least_five_minutes_when_alone(self):
        configuration = self._automatic_leave_configuration(only_participant_timeout=60)

        self.assertEqual(configuration.only_participant_in_meeting_timeout_seconds, 300)

    def test_scheduled_google_meet_preserves_longer_configured_timeout(self):
        configuration = self._automatic_leave_configuration(only_participant_timeout=600)

        self.assertEqual(configuration.only_participant_in_meeting_timeout_seconds, 600)

    def test_manual_google_meet_preserves_configured_timeout(self):
        configuration = self._automatic_leave_configuration(
            join_at=None,
            only_participant_timeout=60,
        )

        self.assertEqual(configuration.only_participant_in_meeting_timeout_seconds, 60)

    def test_scheduled_non_google_meeting_preserves_configured_timeout(self):
        configuration = self._automatic_leave_configuration(
            meeting_type=MeetingTypes.TEAMS,
            only_participant_timeout=60,
        )

        self.assertEqual(configuration.only_participant_in_meeting_timeout_seconds, 60)
