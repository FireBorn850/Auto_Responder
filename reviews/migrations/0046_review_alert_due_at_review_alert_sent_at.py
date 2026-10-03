from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0045_alter_businessprofile_sync_frequency'),
    ]

    operations = [
        migrations.AddField(
            model_name='review',
            name='alert_due_at',
            field=models.DateTimeField(blank=True, null=True, help_text='Negative-review alert held back by quiet hours; sent at this time by the send_due_alerts command.'),
        ),
        migrations.AddField(
            model_name='review',
            name='alert_sent_at',
            field=models.DateTimeField(blank=True, null=True, help_text='When the negative-review alert email went out. Prevents duplicates.'),
        ),
    ]
