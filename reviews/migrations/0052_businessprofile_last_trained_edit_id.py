from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0051_teaminvite_token'),
    ]

    operations = [
        migrations.AddField(
            model_name='businessprofile',
            name='last_trained_edit_id',
            field=models.PositiveIntegerField(
                blank=True, null=True,
                help_text='Newest EditLog id the AI training has learned from; the nightly run skips if nothing is newer.',
            ),
        ),
    ]
