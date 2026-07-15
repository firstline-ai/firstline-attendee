import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "attendee.settings.development")

import django

django.setup()

from unittest import TestCase
from unittest.mock import MagicMock, patch

from bots.google_meet_bot_adapter.google_meet_ui_methods import GoogleMeetUIMethods


class MinimalGoogleMeetAdapter(GoogleMeetUIMethods):
    def __init__(self):
        self.google_meet_bot_login_is_available = False
        self.google_meet_bot_login_should_be_used = False
        self.meeting_url = "https://meet.google.com/abc-defg-hij"
        self.driver = MagicMock()
        self.disable_incoming_video = False
        self.google_meet_closed_captions_language = None
        self.upsert_caption_callback = None
        self.ready_to_show_bot_image = MagicMock(return_value=None)


class TestGoogleMeetUIMethods(TestCase):
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.set_layout", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.click_captions_button", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.click_element", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.locate_element", return_value=MagicMock())
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.turn_off_media_inputs", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.fill_out_name_input", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.get_layout_to_select", return_value=None)
    def test_audio_only_join_does_not_require_captions_button(
        self,
        mock_get_layout_to_select,
        mock_check_if_meeting_is_found,
        mock_fill_out_name_input,
        mock_turn_off_media_inputs,
        mock_locate_element,
        mock_click_element,
        mock_click_captions_button,
        mock_wait_for_host_if_needed,
        mock_set_layout,
    ):
        adapter = MinimalGoogleMeetAdapter()
        adapter.disable_incoming_video = True
        adapter.wait_until_admitted_to_meeting_without_captions = MagicMock(return_value=None)

        adapter.attempt_to_join_meeting()

        mock_click_captions_button.assert_not_called()
        adapter.wait_until_admitted_to_meeting_without_captions.assert_called_once()
        mock_set_layout.assert_not_called()

    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.ready_to_show_bot_image", create=True, return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.set_layout", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.wait_for_host_if_needed", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.click_captions_button", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.click_element", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.locate_element", return_value=MagicMock())
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.turn_off_media_inputs", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.fill_out_name_input", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.check_if_meeting_is_found", return_value=None)
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.GoogleMeetUIMethods.get_layout_to_select", return_value=None)
    def test_google_meet_closed_caption_transcription_still_enables_captions(
        self,
        mock_get_layout_to_select,
        mock_check_if_meeting_is_found,
        mock_fill_out_name_input,
        mock_turn_off_media_inputs,
        mock_locate_element,
        mock_click_element,
        mock_click_captions_button,
        mock_wait_for_host_if_needed,
        mock_set_layout,
        mock_ready_to_show_bot_image,
    ):
        adapter = MinimalGoogleMeetAdapter()
        adapter.upsert_caption_callback = MagicMock()
        adapter.wait_until_admitted_to_meeting_without_captions = MagicMock(return_value=None)

        adapter.attempt_to_join_meeting()

        mock_click_captions_button.assert_called_once()
        adapter.wait_until_admitted_to_meeting_without_captions.assert_not_called()
        mock_set_layout.assert_called_once_with(None)

    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.WebDriverWait")
    def test_wait_until_admitted_without_captions_returns_when_leave_button_is_available(self, MockWebDriverWait):
        adapter = MinimalGoogleMeetAdapter()
        adapter.automatic_leave_configuration = MagicMock(waiting_room_timeout_seconds=600)
        MockWebDriverWait.return_value.until.return_value = MagicMock()

        adapter.wait_until_admitted_to_meeting_without_captions()

        MockWebDriverWait.assert_called_once_with(adapter.driver, 1)
