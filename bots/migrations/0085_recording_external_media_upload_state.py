from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0084_botmediarequest_mute_video"),
    ]

    operations = [
        migrations.AddField(
            model_name="recording",
            name="external_media_upload_state",
            field=models.IntegerField(
                choices=[
                    (1, "Not Requested"),
                    (2, "Pending"),
                    (3, "Uploading"),
                    (4, "Complete"),
                    (5, "Failed"),
                ],
                default=1,
            ),
        ),
        migrations.AddField(
            model_name="recording",
            name="external_media_upload_attempt_count",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="recording",
            name="external_media_upload_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recording",
            name="external_media_upload_enqueued_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recording",
            name="external_media_upload_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recording",
            name="external_media_upload_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recording",
            name="external_media_upload_failure_data",
            field=models.JSONField(default=None, null=True),
        ),
    ]
