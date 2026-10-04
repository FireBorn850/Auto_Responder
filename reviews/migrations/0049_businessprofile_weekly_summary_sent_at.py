from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0048_billing_polar'),
    ]

    operations = [
        migrations.AddField(
            model_name='businessprofile',
            name='weekly_summary_sent_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='When the last weekly summary email went out; stops a re-run from sending it twice.',
            ),
        ),
    ]
