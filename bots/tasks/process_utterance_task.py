import json
import logging
import os
import time

import requests
from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from bots.models import Credentials, RecordingManager, TranscriptionFailureReasons, TranscriptionProviders, Utterance, WebhookTriggerTypes
from bots.transcription_utils import get_transcription_via_assemblyai_from_mp3, is_retryable_failure
from bots.utils import pcm_to_mp3
from bots.webhook_payloads import utterance_webhook_payload
from bots.webhook_utils import trigger_webhook

logger = logging.getLogger(__name__)

PCM_BYTES_PER_SAMPLE = 2
DEFAULT_CUSTOM_ASYNC_MAX_CHUNK_DURATION_SECONDS = 25
TRANSCRIPTION_CLAIM_STALE_SECONDS = int(os.getenv("TRANSCRIPTION_CLAIM_STALE_SECONDS", 900))


def transform_diarized_json_to_schema(result):
    """
    Transform OpenAI diarized_json format to Attendee's expected transcription schema.
    """
    transcription = {"transcript": result.get("text", "")}

    # Extract segments (OpenAI sends each "word" as a separate segment, may contain multiple words).
    # We will transform each segment into a word object, despite the fact that it may contain multiple words.
    segments = result.get("segments", [])
    words = []

    for segment in segments:
        segment_text = segment.get("text", "")
        segment_start = segment.get("start", 0.0)
        segment_end = segment.get("end", segment_start)
        speaker = segment.get("speaker", None)

        word_obj = {
            "word": segment_text,
            "start": segment_start,
            "end": segment_end,
            "speaker": speaker,
        }
        words.append(word_obj)

    if words:
        transcription["words"] = words

    return transcription


def get_transcription(utterance):
    try:
        # Regular transcription providers that support async transcription
        if utterance.transcription_provider == TranscriptionProviders.DEEPGRAM:
            transcription, failure_data = get_transcription_via_deepgram(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.GLADIA:
            transcription, failure_data = get_transcription_via_gladia(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.OPENAI:
            transcription, failure_data = get_transcription_via_openai(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.ASSEMBLY_AI:
            transcription, failure_data = get_transcription_via_assemblyai(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.SARVAM:
            transcription, failure_data = get_transcription_via_sarvam(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.ELEVENLABS:
            transcription, failure_data = get_transcription_via_elevenlabs(utterance)
        elif utterance.transcription_provider == TranscriptionProviders.CUSTOM_ASYNC:
            transcription, failure_data = get_transcription_via_custom_async(utterance)
        else:
            raise Exception(f"Unknown or streaming-only transcription provider: {utterance.transcription_provider}")

        return transcription, failure_data
    except Exception as e:
        return None, {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}


@shared_task(
    bind=True,
    soft_time_limit=3600,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def process_utterance(self, utterance_id):
    task_id = str(self.request.id or f"inline-{utterance_id}")
    now = timezone.now()
    stale_cutoff = now - timezone.timedelta(seconds=TRANSCRIPTION_CLAIM_STALE_SECONDS)
    claimed = (
        Utterance.objects.filter(
            id=utterance_id,
            transcription__isnull=True,
            failure_data__isnull=True,
        )
        .filter(Q(transcription_processing_task_id__isnull=True) | Q(transcription_processing_task_id=task_id) | Q(transcription_processing_started_at__lte=stale_cutoff))
        .update(
            transcription_processing_task_id=task_id,
            transcription_processing_started_at=now,
        )
    )
    if not claimed:
        logger.info("Utterance %s already has a completed transcription or active claim", utterance_id)
        return

    utterance = Utterance.objects.select_related("recording__bot", "audio_chunk").get(id=utterance_id)
    logger.info(f"Processing utterance {utterance_id}")

    recording = utterance.recording

    if utterance.failure_data:
        logger.info(f"process_utterance was called for utterance {utterance_id} but it has already failed, skipping")
        return

    if utterance.transcription is None:
        utterance.transcription_attempt_count += 1

        transcription, failure_data = get_transcription(utterance)

        if failure_data:
            if utterance.transcription_attempt_count < 5 and is_retryable_failure(failure_data):
                utterance.save()
                raise Exception(f"Retryable failure when transcribing utterance {utterance_id}: {failure_data}")
            else:
                # Keep the audio blob around if it fails
                utterance.failure_data = failure_data
                utterance.save()
                logger.info(f"Transcription failed for utterance {utterance_id}, failure data: {failure_data}")
                return

        # The direct audio_blob column on the utterance model is deprecated, but for backwards compatibility, we need to clear it if it exists
        if utterance.audio_blob:
            utterance.audio_blob = b""  # set the audio blob binary field to empty byte string

        # If the utterance has an associated audio chunk, clear the audio blob on the audio chunk.
        # If async transcription data is being saved, do NOT clear it, because we may use it later in an async transcription.
        if utterance.audio_chunk and not utterance.recording.bot.record_async_transcription_audio_chunks():
            utterance.audio_chunk.clear_audio_data()

        utterance.transcription = transcription
        utterance.transcription_processing_task_id = None
        utterance.transcription_processing_started_at = None
        utterance.save()

        logger.info(f"Transcription complete for utterance {utterance_id}")

        # Don't send webhook for empty transcript or an async transcription
        if utterance.transcription.get("transcript") and utterance.async_transcription is None:
            trigger_webhook(
                webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE,
                bot=recording.bot,
                payload=utterance_webhook_payload(utterance),
            )

    # If the utterance is for an async transcription, we don't need to do anything with the recording state.
    if utterance.async_transcription is not None:
        return

    # If the recording is in a terminal state and there are no more utterances to transcribe, set the recording's transcription state to complete
    if RecordingManager.is_terminal_state(utterance.recording.state) and Utterance.objects.filter(recording=utterance.recording, transcription__isnull=True).count() == 0:
        RecordingManager.set_recording_transcription_complete(utterance.recording)


def get_transcription_via_gladia(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    gladia_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.GLADIA).first()
    if not gladia_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    gladia_credentials = gladia_credentials_record.get_credentials()
    if not gladia_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    upload_url = "https://api.gladia.io/v2/upload"

    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate())
    headers = {
        "x-gladia-key": gladia_credentials["api_key"],
    }
    files = {"audio": ("file.mp3", payload_mp3, "audio/mpeg")}
    upload_response = requests.request("POST", upload_url, headers=headers, files=files)

    if upload_response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if upload_response.status_code != 200 and upload_response.status_code != 201:
        return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "status_code": upload_response.status_code}

    upload_response_json = upload_response.json()
    audio_url = upload_response_json["audio_url"]

    transcribe_url = "https://api.gladia.io/v2/pre-recorded"
    transcribe_request_body = {"audio_url": audio_url}
    if transcription_settings.gladia_enable_code_switching():
        transcribe_request_body["enable_code_switching"] = True
        transcribe_request_body["code_switching_config"] = {
            "languages": transcription_settings.gladia_code_switching_languages(),
        }
    transcribe_response = requests.request("POST", transcribe_url, headers=headers, json=transcribe_request_body)

    if transcribe_response.status_code != 200 and transcribe_response.status_code != 201:
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_request", "status_code": transcribe_response.status_code}

    transcribe_response_json = transcribe_response.json()
    result_url = transcribe_response_json["result_url"]

    # Poll the result_url until we get a completed transcription
    max_retries = 120  # Maximum number of retries (2 minutes with 1s sleep)
    retry_count = 0

    while retry_count < max_retries:
        result_response = requests.get(result_url, headers=headers)

        if result_response.status_code != 200:
            logger.error(f"Gladia result fetch failed with status code {result_response.status_code}")
            time.sleep(10)
            retry_count += 1
            continue

        result_data = result_response.json()
        status = result_data.get("status")

        if status == "done":
            # Transcription is complete
            transcription = result_data.get("result", {}).get("transcription", "")
            logger.info("Gladia transcription completed successfully, now deleting audio file from Gladia")
            # Delete the audio file from Gladia
            delete_response = requests.request("DELETE", result_url, headers=headers)
            if delete_response.status_code != 200 and delete_response.status_code != 202:
                logger.error(f"Gladia delete failed with status code {delete_response.status_code}")
            else:
                logger.info("Gladia delete successful")

            transcription["transcript"] = transcription["full_transcript"]
            del transcription["full_transcript"]

            # Extract all words from all utterances into a flat list
            all_words = []
            for utterance in transcription["utterances"]:
                if "words" in utterance:
                    all_words.extend(utterance["words"])
            transcription["words"] = all_words
            del transcription["utterances"]

            return transcription, None

        elif status == "error":
            error_code = result_data.get("error_code")
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error_code": error_code}

        elif status in ["queued", "processing"]:
            # Still processing, wait and retry
            logger.info(f"Gladia transcription status: {status}, waiting...")
            time.sleep(1)
            retry_count += 1

        else:
            # Unknown status
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "status": status}

    # If we've reached here, we've timed out
    return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "step": "transcribe_result_poll"}


def get_transcription_via_deepgram(utterance):
    from deepgram import (
        DeepgramApiError,
        DeepgramClient,
        DeepgramClientOptions,
        FileSource,
        PrerecordedOptions,
    )

    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    payload: FileSource = {
        "buffer": utterance.get_audio_blob().tobytes(),
    }

    deepgram_model = transcription_settings.deepgram_model()

    options = PrerecordedOptions(
        model=deepgram_model,
        smart_format=True,
        language=transcription_settings.deepgram_language(),
        detect_language=transcription_settings.deepgram_detect_language(),
        keyterm=transcription_settings.deepgram_keyterms(),
        keywords=transcription_settings.deepgram_keywords(),
        encoding="linear16",  # for 16-bit PCM
        sample_rate=utterance.get_sample_rate(),
        redact=transcription_settings.deepgram_redaction_settings(),
        replace=transcription_settings.deepgram_replace_settings(),
    )

    deepgram_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.DEEPGRAM).first()
    if not deepgram_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    deepgram_credentials = deepgram_credentials_record.get_credentials()
    if not deepgram_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    deepgram_base_url = transcription_settings.deepgram_base_url()
    if deepgram_base_url:
        logger.info(f"Using Deepgram base URL {deepgram_base_url} for transcription")
        config = DeepgramClientOptions(url=deepgram_base_url)
        deepgram = DeepgramClient(deepgram_credentials["api_key"], config)
    else:
        deepgram = DeepgramClient(deepgram_credentials["api_key"])

    try:
        response = deepgram.listen.rest.v("1").transcribe_file(payload, options)
    except DeepgramApiError as e:
        original_error_json = json.loads(e.original_error)
        if original_error_json.get("err_code") == "INVALID_AUTH":
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error_code": original_error_json.get("err_code"), "error_json": original_error_json}

    logger.info(f"Deepgram transcription complete with model {deepgram_model}")
    alternatives = response.results.channels[0].alternatives
    if len(alternatives) == 0:
        logger.info(f"Deepgram transcription with model {deepgram_model} had no alternatives, returning empty transcription")
        return {"transcript": "", "words": []}, None
    return json.loads(alternatives[0].to_json()), None


def get_transcription_via_openai(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    openai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not openai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    openai_credentials = openai_credentials_record.get_credentials()
    if not openai_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    # If the audio blob is less than 80ms in duration, just return an empty transcription
    # Audio clips this short are almost never generated, it almost certainly didn't have any speech
    # and if we send it to the openai api, it will fail with a corrupted file error
    if utterance.duration_ms < 80:
        logger.info(f"OpenAI transcription skipped for utterance {utterance.id} because it's less than 80ms in duration")
        return {"transcript": ""}, None

    # Convert PCM audio to MP3
    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate())

    # Prepare the request for OpenAI's transcription API
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base_url}/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {openai_credentials['api_key']}",
    }
    files = {"file": ("file.mp3", payload_mp3, "audio/mpeg"), "model": (None, transcription_settings.openai_transcription_model())}
    if transcription_settings.openai_transcription_prompt():
        files["prompt"] = (None, transcription_settings.openai_transcription_prompt())
    if transcription_settings.openai_transcription_language():
        files["language"] = (None, transcription_settings.openai_transcription_language())
    # Add response_format and chunking_strategy for gpt-4o-transcribe-diarize
    response_format = transcription_settings.openai_transcription_response_format()
    if response_format:
        files["response_format"] = (None, response_format)
    chunking_strategy = transcription_settings.openai_transcription_chunking_strategy()
    if chunking_strategy:
        # If chunking_strategy is a dict (server_vad object), JSON stringify it
        if isinstance(chunking_strategy, dict):
            files["chunking_strategy"] = (None, json.dumps(chunking_strategy))
        else:
            files["chunking_strategy"] = (None, chunking_strategy)

    response = requests.post(url, headers=headers, files=files)

    if response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if response.status_code != 200:
        logger.error(f"OpenAI transcription failed with status code {response.status_code}: {response.text}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "response_text": response.text}

    result = response.json()
    logger.info(f"OpenAI transcription completed successfully for utterance {utterance.id}.")

    # If diarized_json format, transform to Attendee's expected transcription schema
    if response_format == "diarized_json":
        transcription = transform_diarized_json_to_schema(result)
    else:
        transcription = {"transcript": result.get("text", "")}

    return transcription, None


def get_transcription_via_assemblyai(utterance):
    return get_transcription_via_assemblyai_from_mp3(
        retrieve_mp3_data_callback=lambda: pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate()),
        duration_ms=utterance.duration_ms,
        identifier=f"utterance {utterance.id}",
        transcription_settings=utterance.transcription_settings,
        recording=utterance.recording,
    )


def get_transcription_via_sarvam(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    sarvam_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.SARVAM).first()
    if not sarvam_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    sarvam_credentials = sarvam_credentials_record.get_credentials()
    if not sarvam_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = sarvam_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    headers = {"api-subscription-key": api_key}
    base_url = "https://api.sarvam.ai/speech-to-text"

    # If the audio blob is less than 50ms in duration, just return an empty transcription
    # Audio clips this short are almost never generated, it almost certainly didn't have any speech
    # and if we send it to the sarvam api, it will fail
    if utterance.duration_ms < 50:
        logger.info(f"Sarvam transcription skipped for utterance {utterance.id} because it's less than 50ms in duration")
        return {"transcript": ""}, None

    # Sarvam says 16kHz sample rate works best
    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate(), output_sample_rate=16000)

    files = {"file": ("audio.mp3", payload_mp3, "audio/mpeg")}

    # Add optional parameters if configured
    data = {}
    if transcription_settings.sarvam_language_code():
        data["language_code"] = transcription_settings.sarvam_language_code()
    if transcription_settings.sarvam_model():
        data["model"] = transcription_settings.sarvam_model()

    try:
        response = requests.post(base_url, headers=headers, files=files, data=data if data else None)

        if response.status_code == 403:
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

        if response.status_code == 429:
            return None, {"reason": TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED, "status_code": response.status_code}

        if response.status_code != 200:
            logger.error(f"Sarvam transcription failed with status code {response.status_code}: {response.text}")
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "response_text": response.text}

        result = response.json()
        logger.info("Sarvam transcription completed successfully")

        # Extract transcript from the response
        transcript_text = result.get("transcript", "")

        # Format the response to match our expected schema
        transcription = {"transcript": transcript_text}

        return transcription, None

    except requests.exceptions.RequestException as e:
        logger.error(f"Sarvam transcription request failed: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": str(e)}
    except json.JSONDecodeError as e:
        logger.error(f"Sarvam transcription response parsing failed: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": f"Invalid JSON response: {str(e)}"}
    except Exception as e:
        logger.error(f"Sarvam transcription unexpected error: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}


def get_transcription_via_elevenlabs(utterance):
    recording = utterance.recording
    transcription_settings = utterance.transcription_settings
    elevenlabs_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.ELEVENLABS).first()
    if not elevenlabs_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    elevenlabs_credentials = elevenlabs_credentials_record.get_credentials()
    if not elevenlabs_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = elevenlabs_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    # Convert PCM audio to MP3 for ElevenLabs
    payload_mp3 = pcm_to_mp3(utterance.get_audio_blob().tobytes(), sample_rate=utterance.get_sample_rate())

    # Prepare the request for ElevenLabs speech-to-text API
    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {
        "xi-api-key": api_key,
    }

    # Prepare multipart form data
    files = {"file": ("audio.mp3", payload_mp3, "audio/mpeg")}

    # Add model_id if configured
    data = {}
    if transcription_settings.elevenlabs_model_id():
        data["model_id"] = transcription_settings.elevenlabs_model_id()

    if transcription_settings.elevenlabs_language_code():
        data["language_code"] = transcription_settings.elevenlabs_language_code()

    data["tag_audio_events"] = transcription_settings.elevenlabs_tag_audio_events()

    try:
        response = requests.post(url, headers=headers, files=files, data=data if data else None)

        if response.status_code == 401:
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

        if response.status_code == 429:
            return None, {"reason": TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED, "status_code": response.status_code}

        if response.status_code != 200:
            logger.error(f"ElevenLabs transcription failed with status code {response.status_code}: {response.text}")
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "response_text": response.text}

        result = response.json()
        logger.info("ElevenLabs transcription completed successfully")

        if result.get("language_probability", 0.0) < 0.5:
            logger.info(f"ElevenLabs transcription skipped for utterance {utterance.id} because the language probability was less than 0.5")
            return {"transcript": "", "words": []}, None

        # Extract transcript and words from the response
        transcript_text = result.get("text", "")
        words = list(map(lambda word: {"word": word.get("text"), "start": word.get("start"), "end": word.get("end")}, result.get("words", [])))

        # Format the response to match our expected schema
        transcription = {"transcript": transcript_text, "words": words, "language": result.get("language_code", None)}

        return transcription, None

    except requests.exceptions.RequestException as e:
        logger.error(f"ElevenLabs transcription request failed: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": str(e)}
    except json.JSONDecodeError as e:
        logger.error(f"ElevenLabs transcription response parsing failed: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": f"Invalid JSON response: {str(e)}"}
    except Exception as e:
        logger.error(f"ElevenLabs transcription unexpected error: {str(e)}")
        return None, {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}


def custom_async_pcm_chunks(pcm_audio, sample_rate, max_chunk_duration_seconds):
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if max_chunk_duration_seconds <= 0:
        raise ValueError("max_chunk_duration_seconds must be positive")

    bytes_per_second = sample_rate * PCM_BYTES_PER_SAMPLE
    max_chunk_bytes = bytes_per_second * max_chunk_duration_seconds
    max_chunk_bytes -= max_chunk_bytes % PCM_BYTES_PER_SAMPLE

    if len(pcm_audio) <= max_chunk_bytes:
        return [(0.0, pcm_audio)]

    chunks = []
    for start_byte in range(0, len(pcm_audio), max_chunk_bytes):
        offset_seconds = start_byte / bytes_per_second
        chunks.append((offset_seconds, pcm_audio[start_byte : start_byte + max_chunk_bytes]))
    return chunks


def combine_custom_async_transcriptions(transcriptions_with_offsets):
    transcript_parts = []
    words = []

    for offset_seconds, transcription in transcriptions_with_offsets:
        transcript = transcription.get("transcript")
        if transcript:
            transcript_parts.append(transcript)

        for word in transcription.get("words", []):
            adjusted_word = word.copy()
            if "start" in adjusted_word:
                adjusted_word["start"] = round(float(adjusted_word["start"]) + offset_seconds, 3)
            if "end" in adjusted_word:
                adjusted_word["end"] = round(float(adjusted_word["end"]) + offset_seconds, 3)
            words.append(adjusted_word)

    return {"transcript": " ".join(transcript_parts).strip(), "words": words}


def transcribe_custom_async_mp3(base_url, payload_mp3, data, timeout):
    files = {"audio": ("audio.mp3", payload_mp3, "audio/mpeg")}

    try:
        logger.info("Sending audio to custom async service at %s", base_url)
        response = requests.post(base_url, files=files, data=data if data else None, timeout=timeout)

        if response.status_code == 401:
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

        if response.status_code == 429:
            return None, {"reason": TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED, "status_code": response.status_code}

        if response.status_code != 200:
            logger.error("Custom async transcription failed with status code %s", response.status_code)
            return None, {
                "reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED,
                "status_code": response.status_code,
                "response_text": response.text,
            }

        result_data = response.json()
        logger.info("Custom async transcription request completed")

        status = result_data.get("status")
        if status == "done":
            transcription = result_data.get("result", {}).get("transcription", "")
            logger.info("Custom async transcription completed successfully")
            transcription["transcript"] = transcription["full_transcript"]
            del transcription["full_transcript"]

            all_words = []
            for utterance in transcription["utterances"]:
                if "words" in utterance:
                    all_words.extend(utterance["words"])
            transcription["words"] = all_words
            del transcription["utterances"]

            return transcription, None

        if status == "error":
            error_code = result_data.get("error_code")
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error_code": error_code}

        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "status": status}

    except requests.exceptions.Timeout:
        logger.error("Custom async transcription request timed out after %s seconds", timeout)
        return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "timeout": timeout}
    except requests.exceptions.RequestException as e:
        logger.error("Custom async transcription request failed: %s", e)
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": str(e)}
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("Custom async transcription response parsing failed: %s", e)
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": f"Invalid JSON response: {e}"}
    except Exception as e:
        logger.error("Custom async transcription unexpected error: %s", e)
        return None, {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}


def custom_async_max_chunk_duration_seconds():
    configured_value = os.getenv("CUSTOM_ASYNC_MAX_CHUNK_DURATION_SECONDS", str(DEFAULT_CUSTOM_ASYNC_MAX_CHUNK_DURATION_SECONDS))
    try:
        duration_seconds = int(configured_value)
    except (TypeError, ValueError) as error:
        raise ValueError("CUSTOM_ASYNC_MAX_CHUNK_DURATION_SECONDS must be a positive integer") from error

    if duration_seconds <= 0:
        raise ValueError("CUSTOM_ASYNC_MAX_CHUNK_DURATION_SECONDS must be a positive integer")

    return duration_seconds


def get_transcription_via_custom_async(utterance):
    transcription_settings = utterance.transcription_settings

    # Get the base URL from environment variable
    base_url = os.getenv("CUSTOM_ASYNC_TRANSCRIPTION_URL")
    if not base_url:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "CUSTOM_ASYNC_TRANSCRIPTION_URL environment variable not set"}

    # Get additional properties from settings
    additional_props = transcription_settings.custom_async_additional_props()

    # Add additional properties as form data
    data = {}
    for key, value in additional_props.items():
        if isinstance(value, (dict, list)):
            data[key] = json.dumps(value)
        else:
            data[key] = value

    # Get timeout from environment or use default (120 retries like Gladia and AssemblyAI)
    timeout = int(os.getenv("CUSTOM_ASYNC_TRANSCRIPTION_TIMEOUT", "120"))  # 120 seconds default timeout
    try:
        max_chunk_duration_seconds = custom_async_max_chunk_duration_seconds()
    except ValueError as error:
        logger.error("Custom async transcription configuration error: %s", error)
        return None, {"reason": TranscriptionFailureReasons.CONFIGURATION_ERROR, "error": str(error)}

    pcm_audio = utterance.get_audio_blob().tobytes()
    sample_rate = utterance.get_sample_rate()
    chunks = custom_async_pcm_chunks(pcm_audio, sample_rate, max_chunk_duration_seconds)

    if len(chunks) > 1:
        logger.info(
            "Splitting custom async utterance %s into %s chunks of up to %ss",
            utterance.id,
            len(chunks),
            max_chunk_duration_seconds,
        )

    transcriptions_with_offsets = []
    for offset_seconds, pcm_chunk in chunks:
        payload_mp3 = pcm_to_mp3(pcm_chunk, sample_rate=sample_rate)
        transcription, failure_data = transcribe_custom_async_mp3(base_url, payload_mp3, data, timeout)
        if failure_data:
            return None, failure_data
        transcriptions_with_offsets.append((offset_seconds, transcription))

    return combine_custom_async_transcriptions(transcriptions_with_offsets), None
