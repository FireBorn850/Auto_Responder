from django.db import migrations


class Migration(migrations.Migration):
    """google_business_token was never written; the GBP connection uses the encrypted refresh token."""

    dependencies = [
        ('reviews', '0054_businessprofile_business_switch'),
    ]

    operations = [
        migrations.RemoveField(model_name='businessprofile', name='google_business_token'),
    ]
