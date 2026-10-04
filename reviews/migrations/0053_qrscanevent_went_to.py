from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0052_businessprofile_last_trained_edit_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='qrscanevent',
            name='resulted_in_rating',
            field=models.IntegerField(blank=True, help_text='Star tapped on the Smart Feedback Router, if any.', null=True),
        ),
        migrations.AddField(
            model_name='qrscanevent',
            name='went_to',
            field=models.CharField(
                blank=True, default='', max_length=10,
                choices=[('google', 'Google review'), ('private', 'Private feedback')],
                help_text='Where the guest chose to go after rating (every rating can choose Google).',
            ),
        ),
    ]
