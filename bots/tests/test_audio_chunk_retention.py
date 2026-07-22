import unittest
from unittest.mock import patch

from bots.audio_chunk_retention import apply_audio_chunk_retention_policy


class TestAudioChunkRetentionPolicy(unittest.TestCase):
    @patch.dict("os.environ", {"FORCE_RECORD_ASYNC_TRANSCRIPTION_AUDIO_CHUNKS_FOR_CUSTOM_ASYNC": "true"})
    def test_forces_audio_chunk_retention_for_custom_async_transcription(self):
        recording_settings = {
            "format": "none",
            "record_async_transcription_audio_chunks": False,
        }
        transcription_settings = {"custom_async": {"language": "pt-BR"}}

        updated = apply_audio_chunk_retention_policy(recording_settings, transcription_settings)

        self.assertTrue(updated["record_async_transcription_audio_chunks"])
        self.assertFalse(recording_settings["record_async_transcription_audio_chunks"])

    @patch.dict("os.environ", {"FORCE_RECORD_ASYNC_TRANSCRIPTION_AUDIO_CHUNKS_FOR_CUSTOM_ASYNC": "false"})
    def test_does_not_force_audio_chunk_retention_when_feature_flag_is_disabled(self):
        recording_settings = {
            "format": "none",
            "record_async_transcription_audio_chunks": False,
        }
        transcription_settings = {"custom_async": {"language": "pt-BR"}}

        updated = apply_audio_chunk_retention_policy(recording_settings, transcription_settings)

        self.assertIs(updated, recording_settings)
        self.assertFalse(updated["record_async_transcription_audio_chunks"])

    @patch.dict("os.environ", {"FORCE_RECORD_ASYNC_TRANSCRIPTION_AUDIO_CHUNKS_FOR_CUSTOM_ASYNC": "true"})
    def test_does_not_force_audio_chunk_retention_for_non_custom_async_transcription(self):
        recording_settings = {
            "format": "none",
            "record_async_transcription_audio_chunks": False,
        }
        transcription_settings = {"meeting_closed_captions": {"google_meet_language": "pt-BR"}}

        updated = apply_audio_chunk_retention_policy(recording_settings, transcription_settings)

        self.assertIs(updated, recording_settings)
        self.assertFalse(updated["record_async_transcription_audio_chunks"])
