"""
Security: team invites are accepted through a secret link, not by matching email.
Existing invites get a token so the owner can re-send them.
"""
import secrets

from django.db import migrations, models


def give_existing_invites_a_token(apps, schema_editor):
    TeamInvite = apps.get_model('reviews', 'TeamInvite')
    for invite in TeamInvite.objects.filter(token__isnull=True):
        invite.token = secrets.token_urlsafe(32)
        invite.save(update_fields=['token'])


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0050_review_unique_external_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='teaminvite',
            name='token',
            field=models.CharField(
                blank=True, max_length=64, null=True, unique=True,
                help_text='Secret part of the invite link emailed to the invitee. Only someone with access to that inbox has it.',
            ),
        ),
        migrations.AddField(
            model_name='teaminvite',
            name='accepted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='teaminvite',
            name='linked_user',
            field=models.OneToOneField(
                blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name='team_membership',
                to='auth.user',
                help_text='Set when the invitee opens their secret invite link (or signs in with that verified email).',
            ),
        ),
        migrations.AlterField(
            model_name='activitylog',
            name='action',
            field=models.CharField(choices=[('settings_updated', 'Updated AI Settings'), ('review_approved', 'Approved a Review Reply'), ('team_invite_sent', 'Sent Team Invite'), ('team_invite_revoked', 'Revoked Team Invite'), ('team_invite_accepted', 'Team Invite Accepted')], max_length=30),
        ),
        migrations.RunPython(give_existing_invites_a_token, migrations.RunPython.noop),
    ]
