import os


def should_force_audio_chunk_retention(transcription_settings):
    if os.getenv("FORCE_RECORD_ASYNC_TRANSCRIPTION_AUDIO_CHUNKS_FOR_CUSTOM_ASYNC", "false").lower() != "true":
        return False

    return isinstance(transcription_settings, dict) and "custom_async" in transcription_settings


def apply_audio_chunk_retention_policy(recording_settings, transcription_settings):
    if not should_force_audio_chunk_retention(transcription_settings):
        return recording_settings

    updated_recording_settings = dict(recording_settings or {})
    updated_recording_settings["record_async_transcription_audio_chunks"] = True
    return updated_recording_settings
