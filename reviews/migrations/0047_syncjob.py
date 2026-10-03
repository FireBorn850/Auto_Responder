import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0046_review_alert_due_at_review_alert_sent_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SyncJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('platform', models.CharField(choices=[('google', 'Google'), ('tripadvisor', 'TripAdvisor')], max_length=20)),
                ('state', models.CharField(choices=[('waiting', 'Waiting for data'), ('drafting', 'Drafting replies'), ('done', 'Done'), ('failed', 'Failed')], default='waiting', max_length=10)),
                ('task_id', models.CharField(blank=True, max_length=100)),
                ('business_name', models.CharField(blank=True, max_length=255)),
                ('draft_queue', models.JSONField(blank=True, default=list)),
                ('imported_count', models.PositiveIntegerField(default=0)),
                ('already_answered_count', models.PositiveIntegerField(default=0)),
                ('detail', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sync_jobs', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
