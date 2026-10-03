from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def give_existing_accounts_a_trial(apps, schema_editor):
    """Accounts created before billing was enforced get a fresh 14-day trial."""
    BusinessProfile = apps.get_model('reviews', 'BusinessProfile')
    BusinessProfile.objects.filter(trial_ends_at__isnull=True).update(
        trial_ends_at=timezone.now() + timedelta(days=14))


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0047_syncjob'),
    ]

    operations = [
        migrations.AddField(model_name='businessprofile', name='billing_provider',
                            field=models.CharField(blank=True, default='', max_length=20)),
        migrations.AddField(model_name='businessprofile', name='billing_customer_id',
                            field=models.CharField(blank=True, max_length=255, null=True)),
        migrations.AddField(model_name='businessprofile', name='billing_subscription_id',
                            field=models.CharField(blank=True, db_index=True, max_length=255, null=True)),
        migrations.AddField(model_name='businessprofile', name='billing_interval',
                            field=models.CharField(blank=True, default='', help_text='month or year', max_length=10)),
        migrations.AddField(model_name='businessprofile', name='current_period_end',
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='businessprofile', name='past_due_since',
                            field=models.DateTimeField(blank=True, help_text='When a payment first failed; access continues for 7 days.', null=True)),
        migrations.RunPython(give_existing_accounts_a_trial, migrations.RunPython.noop),
    ]
