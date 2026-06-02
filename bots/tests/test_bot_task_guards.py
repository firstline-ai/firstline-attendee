from unittest.mock import patch

from django.test import TestCase

from bots.models import Bot, BotStates, Organization, Project
from bots.tasks.run_bot_in_ephemeral_container_task import run_bot_in_ephemeral_container
from bots.tasks.run_bot_task import run_bot


class BotTaskGuardsTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_run_bot_skips_terminal_state_bot(self):
        bot = Bot.objects.create(
            project=self.project,
            name="Ended Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            state=BotStates.ENDED,
        )

        with patch("bots.tasks.run_bot_task.BotController") as mock_bot_controller:
            result = run_bot.apply(args=[bot.id]).get()

        mock_bot_controller.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "bot_in_terminal_state")

    @patch("bots.tasks.run_bot_in_ephemeral_container_task.docker.from_env")
    def test_ephemeral_launcher_skips_terminal_state_bot_before_docker_access(self, mock_docker_from_env):
        bot = Bot.objects.create(
            project=self.project,
            name="Fatal Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            state=BotStates.FATAL_ERROR,
        )

        result = run_bot_in_ephemeral_container.apply(args=[bot.id]).get()

        mock_docker_from_env.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "bot_in_terminal_state")
