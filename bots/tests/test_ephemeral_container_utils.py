import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "attendee.settings.development")

import django
import docker

django.setup()

from bots.ephemeral_container_utils import terminate_ephemeral_docker_container


class TestEphemeralContainerUtils(unittest.TestCase):
    @patch("bots.ephemeral_container_utils.docker.from_env")
    def test_terminate_ephemeral_docker_container_removes_container(self, mock_from_env):
        bot = MagicMock(id=123)
        bot.ephemeral_container_name.return_value = "bot-123-test"
        container = MagicMock()
        mock_from_env.return_value.containers.get.return_value = container

        removed = terminate_ephemeral_docker_container(bot)

        self.assertTrue(removed)
        mock_from_env.return_value.containers.get.assert_called_once_with("bot-123-test")
        container.remove.assert_called_once_with(force=True)

    @patch("bots.ephemeral_container_utils.docker.from_env")
    def test_terminate_ephemeral_docker_container_ignores_missing_container(self, mock_from_env):
        bot = MagicMock(id=123)
        bot.ephemeral_container_name.return_value = "bot-123-test"
        mock_from_env.return_value.containers.get.side_effect = docker.errors.NotFound("missing")

        removed = terminate_ephemeral_docker_container(bot)

        self.assertFalse(removed)
        mock_from_env.return_value.containers.get.assert_called_once_with("bot-123-test")

    @patch("bots.ephemeral_container_utils.docker.from_env")
    def test_terminate_ephemeral_docker_container_ignores_docker_connection_errors(self, mock_from_env):
        bot = MagicMock(id=123)
        mock_from_env.side_effect = RuntimeError("docker unavailable")

        removed = terminate_ephemeral_docker_container(bot)

        self.assertFalse(removed)
        bot.ephemeral_container_name.assert_not_called()
