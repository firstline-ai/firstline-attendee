from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Bot, BotLogin, BotLoginGroup, BotLoginPlatform, Project


@override_settings(SECURE_SSL_REDIRECT=False)
class CreateBotViewTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization", centicredits=10000)
        self.user = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="testpassword123",
            role=UserRole.ADMIN,
            organization=self.organization,
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.client.force_login(self.user)

    @patch("bots.projects_views.launch_bot")
    def test_dashboard_google_meet_bot_uses_login_when_login_is_available(self, mock_launch_bot):
        login_group = BotLoginGroup.objects.create(
            project=self.project,
            platform=BotLoginPlatform.GOOGLE_MEET,
            name="Google Meet Logins",
        )
        BotLogin.objects.create(
            group=login_group,
            workspace_domain="example.com",
            email="bot@example.com",
        )

        response = self.client.post(
            reverse("bots:create-bot", kwargs={"object_id": self.project.object_id}),
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Meeting Bot",
            },
        )

        self.assertEqual(response.status_code, 200)
        bot = Bot.objects.get(project=self.project, meeting_url="https://meet.google.com/abc-defg-hij")
        self.assertTrue(bot.settings["google_meet_settings"]["use_login"])
        self.assertEqual(bot.settings["google_meet_settings"]["login_mode"], "always")
        self.assertIsNone(bot.settings["google_meet_settings"]["login_group_name"])
        mock_launch_bot.assert_called_once_with(bot)

    @patch("bots.projects_views.launch_bot")
    def test_dashboard_google_meet_bot_does_not_use_login_when_no_login_is_available(self, mock_launch_bot):
        response = self.client.post(
            reverse("bots:create-bot", kwargs={"object_id": self.project.object_id}),
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Meeting Bot",
            },
        )

        self.assertEqual(response.status_code, 200)
        bot = Bot.objects.get(project=self.project, meeting_url="https://meet.google.com/abc-defg-hij")
        self.assertFalse(bot.settings["google_meet_settings"]["use_login"])
        mock_launch_bot.assert_called_once_with(bot)
