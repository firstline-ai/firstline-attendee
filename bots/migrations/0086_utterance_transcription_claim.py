from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0085_recording_external_media_upload_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="utterance",
            name="transcription_processing_task_id",
            field=models.CharField(blank=True, db_index=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="utterance",
            name="transcription_processing_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
