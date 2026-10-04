"""
Bug #8: one provider review = one row per account.

Before adding the database rule, existing duplicates are made safe WITHOUT
deleting anything: in each group of rows sharing (user, external_id) the most
useful row keeps the ID (posted > approved > has a draft > oldest) and the
others lose only their external_id. Empty-string IDs become NULL.
"""
from django.db import migrations, models


STATUS_RANK = {'posted': 0, 'approved': 1}


def clear_duplicate_external_ids(apps, schema_editor):
    Review = apps.get_model('reviews', 'Review')
    Review.objects.filter(external_id='').update(external_id=None)

    groups = (
        Review.objects.exclude(external_id__isnull=True)
        .values('user_id', 'external_id')
        .annotate(n=models.Count('id'))
        .filter(n__gt=1)
    )
    for group in groups:
        rows = list(Review.objects.filter(user_id=group['user_id'], external_id=group['external_id']))
        rows.sort(key=lambda r: (STATUS_RANK.get(r.status, 2), 0 if r.ai_draft_reply else 1, r.id))
        for extra in rows[1:]:
            Review.objects.filter(id=extra.id).update(external_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0049_businessprofile_weekly_summary_sent_at'),
    ]

    operations = [
        migrations.RunPython(clear_duplicate_external_ids, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='review',
            constraint=models.UniqueConstraint(
                condition=models.Q(('external_id__isnull', False), models.Q(('external_id', ''), _negated=True)),
                fields=('user', 'external_id'),
                name='unique_review_per_user_external_id',
            ),
        ),
    ]
