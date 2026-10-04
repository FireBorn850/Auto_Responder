from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0053_qrscanevent_went_to'),
    ]

    operations = [
        migrations.AddField(
            model_name='businessprofile',
            name='business_switch_count',
            field=models.PositiveSmallIntegerField(
                default=0, help_text='How many times the owner switched to a different business (start over).'),
        ),
        migrations.AddField(
            model_name='businessprofile',
            name='last_business_switch_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
