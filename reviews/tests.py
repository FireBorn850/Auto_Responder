"""
Tests for the "approve a reply" flow.

Run with:   python manage.py test reviews
"""
import io
import re
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from reviews.models import BusinessProfile, EditLog, Review, TeamInvite
from reviews.services import gbp_client


class ApproveReplyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw-12345-x")
        self.profile = BusinessProfile.objects.create(user=self.owner, business_name="Cafe Test")
        self.review = Review.objects.create(
            user=self.owner,
            business_name="Cafe Test",
            reviewer_name="Alice",
            rating=5,
            comment="Lovely coffee and friendly staff.",
            ai_draft_reply="Thank you Alice!",
            status="pending",
            source="google",
        )
        self.client.force_login(self.owner)

    def approve(self, text, review=None):
        review = review or self.review
        return self.client.post(reverse("approve_review", args=[review.id]), {"ai_draft_reply": text})

    # --- the core bug -----------------------------------------------------

    def test_approve_saves_status_and_edited_text(self):
        self.approve("Thank you so much, Alice! See you soon.")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "approved")
        self.assertEqual(self.review.ai_draft_reply, "Thank you so much, Alice! See you soon.")

    def test_approve_sets_first_response_time_once(self):
        self.approve("First version")
        self.review.refresh_from_db()
        first = self.review.first_response_at
        self.assertIsNotNone(first)

        self.approve("Second version")
        self.review.refresh_from_db()
        self.assertEqual(self.review.first_response_at, first)

    def test_edit_is_logged_for_ai_training(self):
        self.approve("Thanks Alice, glad you enjoyed it.")
        log = EditLog.objects.get(review=self.review)
        self.assertEqual(log.ai_draft, "Thank you Alice!")
        self.assertEqual(log.final_text, "Thanks Alice, glad you enjoyed it.")

    def test_unchanged_text_is_not_logged(self):
        self.approve("Thank you Alice!")
        self.assertFalse(EditLog.objects.exists())

    def test_i_posted_it_works_after_approving(self):
        self.approve("Thanks Alice!")
        self.client.post(reverse("mark_posted", args=[self.review.id]))
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "posted")
        self.assertIsNotNone(self.review.first_response_at)

    # --- Google Business Profile auto-posting ---------------------------

    def _connect_gbp(self):
        self.profile.google_business_refresh_token = "encrypted"
        self.profile.google_business_location_id = "locations/1"
        self.profile.google_business_account_id = "accounts/1"
        self.profile.save()
        self.review.external_id = "gbp:abc123"
        self.review.save()

    def test_gbp_success_marks_posted(self):
        self._connect_gbp()
        with mock.patch.object(gbp_client, "post_reply", return_value=True) as post:
            self.approve("Thanks Alice!")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[1], "abc123")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "posted")
        self.assertEqual(self.review.ai_draft_reply, "Thanks Alice!")

    def test_gbp_failure_still_saves_as_approved(self):
        self._connect_gbp()
        with mock.patch.object(gbp_client, "post_reply", side_effect=gbp_client.GBPError("boom")):
            self.approve("Thanks Alice!")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "approved")
        self.assertEqual(self.review.ai_draft_reply, "Thanks Alice!")

    # --- guard rails ------------------------------------------------------

    def test_empty_reply_is_rejected(self):
        self.approve("   ")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "pending")
        self.assertEqual(self.review.ai_draft_reply, "Thank you Alice!")

    def test_get_request_changes_nothing(self):
        self.client.get(reverse("approve_review", args=[self.review.id]))
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "pending")

    def test_viewer_cannot_approve(self):
        viewer = User.objects.create_user("viewer", "viewer@example.com", "pw-12345-x")
        TeamInvite.objects.create(owner=self.owner, email=viewer.email, role="viewer", linked_user=viewer)
        self.client.force_login(viewer)
        self.approve("Hacked reply")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "pending")
        self.assertEqual(self.review.ai_draft_reply, "Thank you Alice!")

    def test_reviewer_role_can_approve(self):
        staff = User.objects.create_user("staff", "staff@example.com", "pw-12345-x")
        TeamInvite.objects.create(owner=self.owner, email=staff.email, role="reviewer", linked_user=staff)
        self.client.force_login(staff)
        self.approve("Thanks from the team!")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "approved")

    def test_cannot_approve_another_business_review(self):
        other = User.objects.create_user("other", "other@example.com", "pw-12345-x")
        BusinessProfile.objects.create(user=other, business_name="Other")
        self.client.force_login(other)
        resp = self.approve("Not mine")
        self.assertEqual(resp.status_code, 404)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "pending")


class DashboardReplyStateTests(TestCase):
    """What the owner sees on the dashboard after approving."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw-12345-x")
        self.profile = BusinessProfile.objects.create(
            user=self.owner, business_name="Cafe Test",
            google_business_refresh_token="encrypted",
            google_business_location_id="locations/1",
            google_business_account_id="accounts/1",
        )
        self.client.force_login(self.owner)

    def _review(self, **kw):
        defaults = dict(user=self.owner, business_name="Cafe Test", reviewer_name="Bob", rating=5,
                        comment="Great place", ai_draft_reply="Thanks Bob!", status="pending", source="google")
        defaults.update(kw)
        return Review.objects.create(**defaults)

    def test_posted_review_shows_live_state_not_approve_form(self):
        review = self._review(status="posted")
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("Reply is live on Google", html)
        self.assertNotIn(reverse("approve_review", args=[review.id]), html)

    def test_gbp_review_form_has_auto_post_attribute(self):
        review = self._review(external_id="gbp:xyz")
        html = self.client.get(reverse("dashboard")).content.decode()
        form = re.search(r'<form action="%s"[^>]*>' % re.escape(reverse("approve_review", args=[review.id])), html)
        self.assertIsNotNone(form)
        self.assertIn('data-auto-post="1"', form.group(0))


# =====================================================================
# Bug #2 — automatic drafting and auto-posting
# =====================================================================
import json

from django.core import mail
from django.test import override_settings

from reviews.services import review_pipeline
from reviews.services.review_pipeline import auto_draft_new_reviews, draft_reply


def fake_sentiment(sentiment='positive', spam=False):
    return mock.patch.object(review_pipeline, 'analyze_review_sentiment',
                             return_value={'sentiment': sentiment, 'is_likely_spam': spam})


def fake_draft(text='Thank you for your kind words!'):
    return mock.patch.object(review_pipeline, 'generate_review_draft', return_value=text)


class PipelineTestBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw-12345-x")
        self.profile = BusinessProfile.objects.create(
            user=self.owner, business_name="Cafe Test", automation_mode='positive_only')

    def connect_gbp(self):
        self.profile.google_business_refresh_token = "encrypted"
        self.profile.google_business_location_id = "locations/1"
        self.profile.google_business_account_id = "accounts/1"
        self.profile.save()

    def make_review(self, rating=5, comment="Lovely coffee and friendly staff.", gbp=True, **kw):
        n = Review.objects.count()
        return Review.objects.create(
            user=self.owner, business_name="Cafe Test", reviewer_name=f"Guest {n}",
            rating=rating, comment=comment, status='pending', source='google',
            external_id=f"gbp:id{n}" if gbp else f"df:id{n}", **kw)


class AutoPostRulesTests(PipelineTestBase):
    def test_happy_review_is_drafted_and_posted_when_google_connected(self):
        self.connect_gbp()
        review = self.make_review(rating=5)
        with fake_sentiment(), fake_draft("Thanks!"), mock.patch.object(gbp_client, 'post_reply', return_value=True) as post:
            result = draft_reply(review, self.profile)
        review.refresh_from_db()
        self.assertTrue(result.posted)
        self.assertEqual(review.status, 'posted')
        self.assertEqual(post.call_args.args[1:], (review.external_id[4:], "Thanks!"))
        self.assertIsNotNone(review.first_response_at)

    def test_without_google_connection_reply_waits_as_approved(self):
        review = self.make_review(rating=5)
        with fake_sentiment(), fake_draft(), mock.patch.object(gbp_client, 'post_reply') as post:
            draft_reply(review, self.profile)
        review.refresh_from_db()
        self.assertEqual(review.status, 'approved')
        post.assert_not_called()

    def test_bad_review_never_auto_posts_even_in_hands_free(self):
        self.connect_gbp()
        self.profile.automation_mode = 'all'
        self.profile.save()
        review = self.make_review(rating=2, comment="Cold food and a long wait.")
        with fake_sentiment('negative'), fake_draft(), mock.patch.object(gbp_client, 'post_reply') as post:
            draft_reply(review, self.profile)
        review.refresh_from_db()
        post.assert_not_called()
        self.assertEqual(review.status, 'approved')   # pre-approved: one click to post
        self.assertTrue(review.ai_draft_reply)

    def test_sarcastic_five_star_waits_for_a_human(self):
        self.connect_gbp()
        review = self.make_review(rating=5, comment="Great, only waited an hour for cold soup.")
        with fake_sentiment('negative'), fake_draft(), mock.patch.object(gbp_client, 'post_reply') as post:
            draft_reply(review, self.profile)
        review.refresh_from_db()
        post.assert_not_called()
        self.assertEqual(review.status, 'pending')

    def test_manual_mode_never_posts(self):
        self.connect_gbp()
        self.profile.automation_mode = 'manual'
        self.profile.save()
        review = self.make_review(rating=5)
        with fake_sentiment(), fake_draft(), mock.patch.object(gbp_client, 'post_reply') as post:
            draft_reply(review, self.profile)
        review.refresh_from_db()
        post.assert_not_called()
        self.assertEqual(review.status, 'pending')

    def test_spam_is_flagged_and_not_drafted(self):
        review = self.make_review(rating=5)
        with fake_sentiment(spam=True), fake_draft() as gen:
            result = draft_reply(review, self.profile)
        review.refresh_from_db()
        self.assertEqual(result.code, 'spam')
        self.assertEqual(review.status, 'flagged')
        gen.assert_not_called()

    def test_simulator_reviews_never_post(self):
        self.connect_gbp()
        review = self.make_review(rating=5, is_simulated=True)
        with fake_sentiment(), fake_draft(), mock.patch.object(gbp_client, 'post_reply') as post:
            draft_reply(review, self.profile)
        post.assert_not_called()

    def test_google_error_keeps_the_approved_draft(self):
        self.connect_gbp()
        review = self.make_review(rating=5)
        with fake_sentiment(), fake_draft("Thanks!"), \
                mock.patch.object(gbp_client, 'post_reply', side_effect=gbp_client.GBPError("down")):
            result = draft_reply(review, self.profile)
        review.refresh_from_db()
        self.assertFalse(result.posted)
        self.assertEqual(review.status, 'approved')
        self.assertEqual(review.ai_draft_reply, "Thanks!")


class AutoDraftAfterSyncTests(PipelineTestBase):
    def test_only_newest_five_are_drafted(self):
        reviews = [self.make_review(rating=5) for _ in range(8)]
        with fake_sentiment(), fake_draft():
            queued = auto_draft_new_reviews([r.id for r in reviews])
        self.assertEqual(queued, 5)
        drafted = [r.id for r in reviews if Review.objects.get(id=r.id).ai_draft_reply]
        self.assertEqual(drafted, [r.id for r in reviews[3:]])

    @override_settings(DATAFORSEO_LOGIN='x', DATAFORSEO_PASSWORD='y')
    def test_google_sync_drafts_new_reviews_and_alert_mentions_draft(self):
        from reviews.services import google_importer
        items = [
            {'review_id': 'r1', 'profile_name': 'Ann', 'review_text': 'Wonderful brunch, will come back!',
             'rating': {'value': 5}},
            {'review_id': 'r2', 'profile_name': 'Ben', 'review_text': 'Rude staff and cold food.',
             'rating': {'value': 1}},
            {'review_id': 'r3', 'profile_name': 'Cat', 'review_text': 'Nice place overall.',
             'rating': {'value': 4}, 'owner_answer': 'Thanks Cat!'},
        ]
        with mock.patch.object(google_importer, 'fetch_reviews', return_value=(items, {})), \
                fake_sentiment(), fake_draft("Drafted reply"):
            imported, already = google_importer.fetch_live_google_reviews('', self.owner, "Cafe Test")

        self.assertEqual(imported, 3)
        ann = Review.objects.get(reviewer_name='Ann')
        ben = Review.objects.get(reviewer_name='Ben')
        cat = Review.objects.get(reviewer_name='Cat')
        self.assertEqual(ann.ai_draft_reply, "Drafted reply")
        self.assertEqual(ann.status, 'approved')       # 5★, Smart Guardrail, no Google connection yet
        self.assertEqual(ben.status, 'pending')        # 1★ waits for a human
        self.assertEqual(ben.ai_draft_reply, "Drafted reply")
        self.assertEqual(cat.status, 'posted')         # already answered on Google: left alone
        self.assertFalse(cat.ai_draft_reply)

        self.assertEqual(len(mail.outbox), 1)          # alert only for the 1★ review
        self.assertIn("already waiting", mail.outbox[0].body)


class WebhookAndButtonTests(PipelineTestBase):
    def test_webhook_review_gets_full_pipeline(self):
        url = reverse('google_review_webhook', args=[self.profile.webhook_token])
        with fake_sentiment(), fake_draft("Webhook reply"):
            resp = self.client.post(url, json.dumps({'reviewer_name': 'Dan', 'rating': 5,
                                                     'comment': 'Superb pastries!', 'detected_language': 'en'}),
                                    content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['ai_draft'], "Webhook reply")
        self.assertEqual(Review.objects.get(reviewer_name='Dan').sentiment, 'positive')

    def test_webhook_rejects_bad_rating(self):
        url = reverse('google_review_webhook', args=[self.profile.webhook_token])
        resp = self.client.post(url, json.dumps({'rating': 'lots', 'comment': 'hi there'}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Review.objects.exists())

    def test_generate_button_reports_auto_post(self):
        self.connect_gbp()
        review = self.make_review(rating=5)
        self.client.force_login(self.owner)
        with fake_sentiment(), fake_draft(), mock.patch.object(gbp_client, 'post_reply', return_value=True):
            resp = self.client.post(reverse('generate_draft', args=[review.id]))
        self.assertEqual(resp.json(), {'ok': True, 'posted': True})

    def test_viewer_cannot_generate(self):
        review = self.make_review(rating=5)
        viewer = User.objects.create_user("viewer", "viewer@example.com", "pw-12345-x")
        TeamInvite.objects.create(owner=self.owner, email=viewer.email, role="viewer", linked_user=viewer)
        self.client.force_login(viewer)
        with fake_sentiment(), fake_draft() as gen:
            resp = self.client.post(reverse('generate_draft', args=[review.id]))
        self.assertFalse(resp.json()['ok'])
        gen.assert_not_called()


# =====================================================================
# Bug #3 — renaming the business must not hide its reviews
# =====================================================================
from reviews import views as review_views


class RenameBusinessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw-12345-x")
        self.profile = BusinessProfile.objects.create(
            user=self.owner, business_name="Cafe Luna",
            google_review_url="https://search.google.com/local/writereview?placeid=PLACE123",
            google_maps_url="https://www.google.com/maps?cid=1")
        for i in range(3):
            Review.objects.create(user=self.owner, business_name="Cafe Luna", reviewer_name=f"G{i}",
                                  rating=5, comment="Great", source='google', external_id=f"r{i}")
        Review.objects.create(user=self.owner, business_name="Cafe Luna", reviewer_name="Sim",
                              rating=4, comment="Test", is_simulated=True)
        self.client.force_login(self.owner)

    def sync(self, **data):
        with mock.patch.object(review_views.sync_jobs, 'start_google_sync') as fetch:
            self.client.post(reverse('sync_google_reviews'), data)
        return fetch

    def test_reviews_still_visible_after_rename(self):
        self.sync(business_name="Café Luna Geneva")
        html = self.client.get(reverse('dashboard')).content.decode()
        for i in range(3):
            self.assertIn(f"G{i}", html)

    def test_rename_relabels_reviews_and_keeps_google_link(self):
        fetch = self.sync(business_name="Café Luna Geneva")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.business_name, "Café Luna Geneva")
        self.assertEqual(self.profile.google_review_url,
                         "https://search.google.com/local/writereview?placeid=PLACE123")
        self.assertEqual(Review.objects.filter(user=self.owner, business_name="Café Luna Geneva").count(), 4)
        self.assertEqual(fetch.call_args.args[0].business_name, "Café Luna Geneva")

    def test_old_reviews_with_a_stale_label_are_still_shown(self):
        Review.objects.create(user=self.owner, business_name="Some Old Name", reviewer_name="Legacy",
                              rating=5, comment="Old one", source='google')
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn("Legacy", html)

    def test_switching_business_deletes_old_synced_reviews_only(self):
        self.sync(business_name="Totally Different Bar", switch_business='1')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.business_name, "Totally Different Bar")
        self.assertIsNone(self.profile.google_review_url)
        self.assertFalse(Review.objects.filter(user=self.owner, is_simulated=False).exists())
        self.assertTrue(Review.objects.filter(user=self.owner, is_simulated=True).exists())

    def test_viewer_cannot_sync_or_rename(self):
        viewer = User.objects.create_user("viewer", "viewer@example.com", "pw-12345-x")
        TeamInvite.objects.create(owner=self.owner, email=viewer.email, role="viewer", linked_user=viewer)
        self.client.force_login(viewer)
        fetch = self.sync(business_name="Hijacked")
        fetch.assert_not_called()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.business_name, "Cafe Luna")


@override_settings(DATAFORSEO_LOGIN='x', DATAFORSEO_PASSWORD='y')
class SyncCostTests(TestCase):
    """A rename must not trigger a paid name search or a full 100-review re-import."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw-12345-x")
        self.profile = BusinessProfile.objects.create(
            user=self.owner, business_name="Cafe Luna",
            google_review_url="https://search.google.com/local/writereview?placeid=PLACE123")

    def run_import(self, name, items=()):
        from reviews.services import google_importer
        with mock.patch.object(google_importer, 'fetch_reviews', return_value=(list(items), {})) as fetch, \
                mock.patch.object(google_importer, 'auto_draft_new_reviews'):
            google_importer.fetch_live_google_reviews('', self.owner, name)
        return fetch

    def test_after_rename_sync_uses_saved_place_and_small_depth(self):
        Review.objects.create(user=self.owner, business_name="Cafe Luna", reviewer_name="Ann",
                              rating=5, comment="Great", source='google', external_id='r1')
        fetch = self.run_import("Café Luna Geneva")
        self.assertEqual(fetch.call_args.kwargs['place_id'], 'PLACE123')
        self.assertEqual(fetch.call_args.kwargs['depth'], 10)

    def test_same_review_is_not_imported_twice_after_rename(self):
        Review.objects.create(user=self.owner, business_name="Cafe Luna", reviewer_name="Ann",
                              rating=5, comment="Great", source='google', external_id='r1')
        item = {'review_id': 'r1', 'profile_name': 'Ann', 'review_text': 'Great', 'rating': {'value': 5}}
        self.run_import("Café Luna Geneva", [item])
        self.assertEqual(Review.objects.filter(user=self.owner).count(), 1)

    def test_simulator_reviews_dont_count_as_a_previous_sync(self):
        Review.objects.create(user=self.owner, business_name="Cafe Luna", reviewer_name="Sim",
                              rating=5, comment="Test", source='google', is_simulated=True)
        fetch = self.run_import("Cafe Luna")
        self.assertEqual(fetch.call_args.kwargs['depth'], 100)


# =====================================================================
# Bug #4 — quiet hours must hold alerts back, not crash the sync
# =====================================================================
from datetime import datetime as _dt, time as _time, timedelta as _td
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.utils import timezone as dj_tz

from reviews.tasks import next_opening_if_quiet, send_due_alerts, send_negative_review_alert


class QuietHoursTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw-12345-x")
        self.profile = BusinessProfile.objects.create(
            user=self.owner, business_name="Cafe Luna", quiet_hours_enabled=True,
            timezone_name="Europe/Zurich")
        self.review = Review.objects.create(
            user=self.owner, business_name="Cafe Luna", reviewer_name="Ben", rating=1,
            comment="Cold food and rude staff.", source='google', ai_draft_reply="Sorry Ben...")

    def set_hours(self, start_offset_h, end_offset_h):
        """Business hours relative to the current Zurich time."""
        now = dj_tz.now().astimezone(ZoneInfo("Europe/Zurich"))
        self.profile.business_hours_start = (now + _td(hours=start_offset_h)).time().replace(second=0, microsecond=0)
        self.profile.business_hours_end = (now + _td(hours=end_offset_h)).time().replace(second=0, microsecond=0)
        self.profile.save()

    def test_closed_now_does_not_crash_and_alert_is_saved_for_opening(self):
        self.set_hours(2, 3)   # opens in 2 hours
        send_negative_review_alert(self.review.id)   # old code: endless self-call -> RecursionError
        self.review.refresh_from_db()
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNotNone(self.review.alert_due_at)
        self.assertGreater(self.review.alert_due_at, dj_tz.now() + _td(hours=1, minutes=50))

    def test_open_now_sends_immediately_once(self):
        self.set_hours(-1, 2)
        send_negative_review_alert(self.review.id)
        send_negative_review_alert(self.review.id)   # second call must not resend
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("already waiting", mail.outbox[0].body)
        self.review.refresh_from_db()
        self.assertIsNotNone(self.review.alert_sent_at)

    def test_due_alert_is_sent_at_opening(self):
        self.set_hours(2, 3)
        send_negative_review_alert(self.review.id)
        self.review.refresh_from_db()
        after_opening = self.review.alert_due_at + _td(minutes=5)
        self.assertEqual(send_due_alerts(now=after_opening), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_due_alert_waits_if_still_closed(self):
        self.set_hours(2, 3)
        Review.objects.filter(id=self.review.id).update(alert_due_at=dj_tz.now() - _td(minutes=1))
        self.assertEqual(send_due_alerts(), 0)
        self.assertEqual(len(mail.outbox), 0)
        self.review.refresh_from_db()
        self.assertGreater(self.review.alert_due_at, dj_tz.now())

    def test_no_alert_if_already_answered_while_waiting(self):
        self.set_hours(-1, 2)
        Review.objects.filter(id=self.review.id).update(
            alert_due_at=dj_tz.now() - _td(minutes=1), status='posted')
        self.assertEqual(send_due_alerts(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_bad_timezone_falls_back_instead_of_crashing(self):
        self.profile.timezone_name = "Mars/Olympus"
        self.profile.business_hours_start = _time(9, 0)
        self.profile.business_hours_end = _time(20, 0)
        self.profile.save()
        noon_zurich = _dt(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("Europe/Zurich"))
        self.assertIsNone(next_opening_if_quiet(self.profile, noon_zurich))

    def test_overnight_hours_window(self):
        self.profile.business_hours_start = _time(18, 0)
        self.profile.business_hours_end = _time(2, 0)
        z = ZoneInfo("Europe/Zurich")
        self.assertIsNone(next_opening_if_quiet(self.profile, _dt(2026, 10, 5, 23, 30, tzinfo=z)))
        opening = next_opening_if_quiet(self.profile, _dt(2026, 10, 5, 10, 0, tzinfo=z))
        self.assertEqual((opening.hour, opening.day), (18, 5))

    @override_settings(DATAFORSEO_LOGIN='x', DATAFORSEO_PASSWORD='y')
    def test_sync_finishes_during_quiet_hours(self):
        from reviews.services import google_importer
        self.set_hours(2, 3)
        items = [{'review_id': 'n1', 'profile_name': 'Zoe', 'review_text': 'Terrible service tonight.',
                  'rating': {'value': 1}}]
        with mock.patch.object(google_importer, 'fetch_reviews', return_value=(items, {})), \
                fake_sentiment('negative'), fake_draft("We're sorry Zoe"):
            imported, _ = google_importer.fetch_live_google_reviews('', self.owner, "Cafe Luna")
        self.assertEqual(imported, 1)
        zoe = Review.objects.get(reviewer_name='Zoe')
        self.assertIsNotNone(zoe.alert_due_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_command_runs(self):
        out = io.StringIO()
        call_command('send_due_alerts', stdout=out)
        self.assertIn("Sent 0 due alert(s).", out.getvalue())

    def test_settings_rejects_unknown_timezone(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('update_settings'), {'timezone_name': 'Mars/Olympus',
                                                     'business_hours_start': '09:00', 'business_hours_end': '20:00'})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.timezone_name, "Europe/Zurich")



# =====================================================================
# Bug #5 — manual sync must never block a web request
# =====================================================================
from reviews.models import SyncJob, SyncLog
from reviews.services import dataforseo_importer as dfs
from reviews.services import sync_jobs


def dfs_result(*reviews):
    """A finished DataForSEO Google result with the given (name, stars, text) reviews."""
    return {'place_id': 'PLACE9', 'cid': '42', 'items': [
        {'review_id': f'x{i}', 'profile_name': n, 'rating': {'value': r}, 'review_text': t}
        for i, (n, r, t) in enumerate(reviews)]}


@override_settings(DATAFORSEO_LOGIN='x', DATAFORSEO_PASSWORD='y', DATAFORSEO_MANUAL_PRIORITY=1)
class NonBlockingSyncTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@example.com", "pw-12345-x")
        self.profile = BusinessProfile.objects.create(user=self.owner, business_name="Cafe Luna")
        self.client.force_login(self.owner)

    def start(self):
        with mock.patch.object(dfs, 'post_task', return_value='TASK1') as post, \
                mock.patch.object(dfs, 'wait_for_result') as wait:
            resp = self.client.post(reverse('sync_google_reviews'), {'business_name': 'Cafe Luna'})
        return resp, post, wait

    def poll(self, result=None):
        with mock.patch.object(dfs, 'get_task_result', return_value=result):
            return self.client.post(reverse('sync_status')).json()

    def test_button_only_submits_the_task_and_returns(self):
        resp, post, wait = self.start()
        self.assertEqual(resp.status_code, 302)
        post.assert_called_once()
        wait.assert_not_called()                       # the old code waited up to 90 s here
        job = SyncJob.objects.get()
        self.assertEqual((job.state, job.task_id), ('waiting', 'TASK1'))

    def test_manual_sync_uses_cheaper_normal_priority(self):
        _, post, _ = self.start()
        self.assertEqual(post.call_args.args[1]['priority'], 1)

    def test_dashboard_shows_progress_banner_while_running(self):
        self.start()
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn('id="syncProgressBanner"', html)

    def test_full_flow_step_by_step(self):
        self.start()
        self.assertEqual(self.poll(None)['state'], 'waiting')        # data not ready yet

        result = dfs_result(('Ann', 5, 'Wonderful brunch!'), ('Ben', 1, 'Cold food, rude staff.'))
        data = self.poll(result)                                     # import step
        self.assertEqual(data['state'], 'drafting')
        self.assertEqual(Review.objects.filter(user=self.owner).count(), 2)
        self.assertEqual(len(mail.outbox), 0)                        # alert waits for the draft

        with fake_sentiment(), fake_draft("Drafted!"):
            states = [self.poll()['state'] for _ in range(3)]        # one draft per poll, then finish
        self.assertEqual(states, ['drafting', 'drafting', 'done'])
        self.assertEqual(Review.objects.filter(ai_draft_reply="Drafted!").count(), 2)
        self.assertEqual(len(mail.outbox), 1)                        # 1★ alert, after drafting
        self.assertIn("already waiting", mail.outbox[0].body)
        self.assertEqual(SyncLog.objects.get().status, 'success')
        self.profile.refresh_from_db()
        self.assertIn('PLACE9', self.profile.google_review_url)      # place saved for cheap future syncs
        self.assertEqual(self.poll()['state'], 'idle')

    def test_only_five_drafts_per_sync(self):
        self.start()
        self.poll(dfs_result(*[(f'G{i}', 5, 'Lovely place') for i in range(8)]))
        self.assertEqual(len(SyncJob.objects.get().draft_queue), 5)

    def test_second_sync_is_blocked_while_one_runs(self):
        self.start()
        _, post, _ = self.start()
        post.assert_not_called()
        self.assertEqual(SyncJob.objects.count(), 1)

    def test_only_one_sync_per_hour(self):
        self.start()
        SyncJob.objects.update(state='done')
        _, post, _ = self.start()
        post.assert_not_called()

    def test_failed_task_is_reported(self):
        self.start()
        with mock.patch.object(dfs, 'get_task_result', side_effect=Exception("DataForSEO 40400: not found")):
            data = self.client.post(reverse('sync_status')).json()
        self.assertEqual(data['state'], 'failed')
        self.assertEqual(SyncLog.objects.get().status, 'failed')

    def test_gives_up_after_six_hours(self):
        self.start()
        SyncJob.objects.update(created_at=dj_tz.now() - _td(hours=7))
        self.assertEqual(self.poll(None)['state'], 'failed')

    def test_nightly_run_finishes_abandoned_sync(self):
        self.start()
        with mock.patch.object(dfs, 'get_task_result', return_value=dfs_result(('Ann', 5, 'Great!'))), \
                fake_sentiment(), fake_draft("Night draft"):
            self.assertEqual(sync_jobs.finish_abandoned_jobs(), 1)
        self.assertEqual(SyncJob.objects.get().state, 'done')
        self.assertEqual(Review.objects.get().ai_draft_reply, "Night draft")

    def test_tripadvisor_sync_runs_the_same_way(self):
        with mock.patch.object(dfs, 'post_task', return_value='TA1') as post:
            self.client.post(reverse('sync_tripadvisor_reviews'), {'business_name': 'Cafe Luna'})
        self.assertEqual(post.call_args.args[0], dfs.TRIPADVISOR)
        ta_result = {'url_path': 'Restaurant_Review-g1-d2-Reviews-Cafe_Luna.html', 'items': [
            {'review_id': 't1', 'review_text': 'Charming spot', 'rating': {'value': 4}, 'user_profile': {'name': 'Tom'}}]}
        self.assertEqual(self.poll(ta_result)['state'], 'drafting')
        self.assertEqual(Review.objects.get().source, 'tripadvisor')
        self.profile.refresh_from_db()
        self.assertIn('Cafe_Luna', self.profile.tripadvisor_url)


# =====================================================================
# Bug #6 — billing must actually decide what an account can do (Polar)
# =====================================================================
import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac
import time as _clock

from reviews.models import AccessCode
from reviews.services import billing, polar_billing

WEBHOOK_SECRET = 'whsec_' + _b64.b64encode(b'super-secret-test-key-1234567890').decode()
POLAR_SETTINGS = dict(
    POLAR_ACCESS_TOKEN='polar_oat_test', POLAR_WEBHOOK_SECRET=WEBHOOK_SECRET, POLAR_SERVER='sandbox',
    POLAR_PRODUCT_STARTER_MONTHLY='prod-sm', POLAR_PRODUCT_STARTER_YEARLY='prod-sy',
    POLAR_PRODUCT_PREMIUM_MONTHLY='prod-pm',
)


def make_owner(username="owner", **profile_fields):
    user = User.objects.create_user(username, f"{username}@example.com", "pw-12345-x")
    profile = BusinessProfile.objects.create(user=user, business_name="Cafe Luna", **profile_fields)
    return user, profile


def expire_trial(profile):
    profile.trial_ends_at = dj_tz.now() - _td(days=1)
    profile.save(update_fields=['trial_ends_at'])


def as_paid(profile, plan, status='active', **extra):
    profile.plan = plan
    profile.subscription_status = status
    profile.billing_subscription_id = 'sub_1'
    profile.current_period_end = extra.pop('current_period_end', dj_tz.now() + _td(days=20))
    for k, v in extra.items():
        setattr(profile, k, v)
    expire_trial(profile)
    profile.save()


class AccessRulesTests(TestCase):
    def test_new_account_gets_14_day_premium_trial(self):
        _, profile = make_owner()
        access = billing.get_access(profile)
        self.assertEqual((access.level, access.plan), ('trial', 'premium'))
        self.assertIn(access.days_left, (13, 14))

    def test_after_trial_account_is_read_only(self):
        _, profile = make_owner()
        expire_trial(profile)
        access = billing.get_access(profile)
        self.assertFalse(access.active)
        self.assertFalse(access.can('auto_post'))

    def test_starter_has_basics_but_not_premium_features(self):
        _, profile = make_owner()
        as_paid(profile, 'starter')
        access = billing.get_access(profile)
        self.assertTrue(access.active)
        for feature in billing.PREMIUM_FEATURES:
            self.assertFalse(access.can(feature), feature)

    def test_failed_payment_has_7_days_grace(self):
        _, profile = make_owner()
        as_paid(profile, 'premium', status='past_due', past_due_since=dj_tz.now() - _td(days=3))
        self.assertEqual(billing.get_access(profile).level, 'grace')
        profile.past_due_since = dj_tz.now() - _td(days=8)
        self.assertFalse(billing.get_access(profile).active)

    def test_cancelled_keeps_access_until_period_end(self):
        _, profile = make_owner()
        as_paid(profile, 'premium', status='canceled')
        self.assertEqual(billing.get_access(profile).level, 'paid')
        profile.current_period_end = dj_tz.now() - _td(minutes=1)
        self.assertFalse(billing.get_access(profile).active)


class ReadOnlyCostsNothingTests(TestCase):
    def setUp(self):
        self.owner, self.profile = make_owner()
        expire_trial(self.profile)
        self.client.force_login(self.owner)
        self.review = Review.objects.create(user=self.owner, business_name="Cafe Luna", reviewer_name="Ann",
                                            rating=5, comment="Lovely coffee", ai_draft_reply="Thanks Ann!")

    def test_no_ai_calls(self):
        with fake_sentiment() as sent, fake_draft() as gen:
            result = draft_reply(self.review, self.profile)
        self.assertEqual(result.code, 'no_plan')
        sent.assert_not_called()
        gen.assert_not_called()

    def test_no_paid_sync(self):
        with mock.patch.object(dfs, 'post_task') as post:
            resp = self.client.post(reverse('sync_google_reviews'), {'business_name': 'Cafe Luna'})
        post.assert_not_called()
        self.assertRedirects(resp, reverse('billing'), fetch_redirect_response=False)

    @override_settings(DATAFORSEO_LOGIN='x', DATAFORSEO_PASSWORD='y')
    def test_nightly_sync_skips_read_only_accounts(self):
        from reviews.tasks import poll_google_reviews
        self.profile.sync_frequency = 'daily'
        self.profile.google_maps_url = 'https://www.google.com/maps?cid=1'
        self.profile.save()
        with mock.patch('reviews.services.google_importer.fetch_live_google_reviews') as fetch:
            poll_google_reviews()
        fetch.assert_not_called()

    def test_reviews_stay_visible_and_approvable(self):
        html = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn("Ann", html)
        self.assertIn("Read-only mode", html)
        self.client.post(reverse('approve_review', args=[self.review.id]), {'ai_draft_reply': 'Thanks Ann!'})
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, 'approved')


class StarterLimitsTests(TestCase):
    def setUp(self):
        self.owner, self.profile = make_owner()
        as_paid(self.profile, 'starter')
        self.client.force_login(self.owner)

    def test_tripadvisor_is_premium(self):
        with mock.patch.object(dfs, 'post_task') as post:
            self.client.post(reverse('sync_tripadvisor_reviews'), {'business_name': 'Cafe Luna'})
        post.assert_not_called()

    def test_starter_replies_wait_for_approval_even_in_auto_mode(self):
        self.profile.automation_mode = 'positive_only'
        self.profile.google_business_refresh_token = 'x'
        self.profile.google_business_location_id = 'locations/1'
        self.profile.save()
        review = Review.objects.create(user=self.owner, business_name="Cafe Luna", reviewer_name="Ann",
                                       rating=5, comment="Lovely coffee", external_id='gbp:1')
        with fake_sentiment(), fake_draft(), mock.patch.object(gbp_client, 'post_reply') as post:
            draft_reply(review, self.profile)
        review.refresh_from_db()
        self.assertEqual(review.status, 'pending')
        post.assert_not_called()

    def test_settings_cannot_enable_auto_mode(self):
        self.client.post(reverse('update_settings'), {'automation_mode': 'all', 'timezone_name': 'Europe/Zurich',
                                                     'business_hours_start': '09:00', 'business_hours_end': '20:00'})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.automation_mode, 'manual')

    def test_no_negative_alerts_on_starter(self):
        review = Review.objects.create(user=self.owner, business_name="Cafe Luna", reviewer_name="Ben",
                                       rating=1, comment="Cold food")
        send_negative_review_alert(review.id)
        self.assertEqual(len(mail.outbox), 0)

    def test_qr_creation_is_premium(self):
        self.client.post(reverse('create_qr'), {'name': 'Table 1', 'target_url': 'https://g.page/x'})
        from reviews.models import SmartQRCode
        self.assertFalse(SmartQRCode.objects.exists())


class FoundingPartnerTests(TestCase):
    def setUp(self):
        self.owner, self.profile = make_owner()
        expire_trial(self.profile)
        self.client.force_login(self.owner)

    def test_unapproved_code_is_rejected(self):
        AccessCode.objects.create(code='FOUNDER-AAAAAA', status='pending')
        self.client.post(reverse('redeem_access_code'), {'code': 'FOUNDER-AAAAAA'})
        self.profile.refresh_from_db()
        self.assertNotEqual(self.profile.plan, 'founding_partner')

    def test_approved_code_gives_30_days_premium(self):
        AccessCode.objects.create(code='FOUNDER-BBBBBB', status='approved')
        self.client.post(reverse('redeem_access_code'), {'code': 'FOUNDER-BBBBBB'})
        self.profile.refresh_from_db()
        access = billing.get_access(self.profile)
        self.assertEqual((access.level, access.plan), ('founding', 'premium'))
        self.assertIn(access.days_left, (29, 30))


def signed_post(client, payload):
    body = json.dumps(payload).encode()
    msg_id, ts = 'msg_1', str(int(_clock.time()))
    key = _b64.b64decode(WEBHOOK_SECRET[len('whsec_'):])
    sig = _b64.b64encode(_hmac.new(key, f"{msg_id}.{ts}.".encode() + body, _hashlib.sha256).digest()).decode()
    return client.post(reverse('polar_webhook'), body, content_type='application/json',
                       HTTP_WEBHOOK_ID=msg_id, HTTP_WEBHOOK_TIMESTAMP=ts, HTTP_WEBHOOK_SIGNATURE=f"v1,{sig}")


@override_settings(**POLAR_SETTINGS)
class PolarWebhookTests(TestCase):
    def setUp(self):
        self.owner, self.profile = make_owner()
        expire_trial(self.profile)

    def event(self, type_, status, product='prod-pm', **extra):
        data = {'id': 'sub_123', 'status': status, 'product_id': product,
                'current_period_end': (dj_tz.now() + _td(days=365)).isoformat(),
                'customer': {'id': 'cus_1', 'external_id': str(self.owner.id)}}
        data.update(extra)
        return {'type': type_, 'data': data}

    def test_paid_subscription_unlocks_plan(self):
        resp = signed_post(self.client, self.event('subscription.active', 'active'))
        self.assertEqual(resp.status_code, 202)
        self.profile.refresh_from_db()
        self.assertEqual((self.profile.plan, self.profile.billing_interval), ('premium', 'month'))
        self.assertEqual(billing.get_access(self.profile).level, 'paid')

    def test_bad_signature_is_rejected(self):
        body = json.dumps(self.event('subscription.active', 'active'))
        resp = self.client.post(reverse('polar_webhook'), body, content_type='application/json',
                                HTTP_WEBHOOK_ID='m', HTTP_WEBHOOK_TIMESTAMP=str(int(_clock.time())),
                                HTTP_WEBHOOK_SIGNATURE='v1,forged')
        self.assertEqual(resp.status_code, 403)
        self.profile.refresh_from_db()
        self.assertFalse(billing.get_access(self.profile).active)

    def test_past_due_then_revoked(self):
        signed_post(self.client, self.event('subscription.active', 'active'))
        signed_post(self.client, self.event('subscription.past_due', 'past_due'))
        self.profile.refresh_from_db()
        self.assertEqual(billing.get_access(self.profile).level, 'grace')
        signed_post(self.client, self.event('subscription.revoked', 'canceled', ended_at=dj_tz.now().isoformat()))
        self.profile.refresh_from_db()
        self.assertFalse(billing.get_access(self.profile).active)


@override_settings(**POLAR_SETTINGS)
class CheckoutTests(TestCase):
    def setUp(self):
        self.owner, self.profile = make_owner()
        self.client.force_login(self.owner)

    def test_checkout_redirects_to_polar_with_right_product(self):
        fake = mock.Mock(status_code=201)
        fake.json.return_value = {'url': 'https://sandbox.polar.sh/checkout/abc'}
        with mock.patch('reviews.services.polar_billing.requests.post', return_value=fake) as post:
            resp = self.client.post(reverse('billing_checkout', args=['starter', 'year']))
        self.assertEqual(resp.url, 'https://sandbox.polar.sh/checkout/abc')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['products'], ['prod-sy'])
        self.assertEqual(body['external_customer_id'], str(self.owner.id))
        self.assertIn('sandbox-api.polar.sh', post.call_args.args[0])

    def test_billing_page_renders(self):
        resp = self.client.get(reverse('billing'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "CHF 19")
        self.assertContains(resp, "CHF 39")
        self.assertContains(resp, "CHF 12.42/mo, billed yearly (CHF 149)")

    def test_no_premium_yearly_checkout(self):
        with mock.patch('reviews.services.polar_billing.requests.post') as post:
            self.client.post(reverse('billing_checkout', args=['premium', 'year']))
        post.assert_not_called()

    def test_teammate_cannot_buy(self):
        staff = User.objects.create_user("staff", "staff@example.com", "pw-12345-x")
        TeamInvite.objects.create(owner=self.owner, email=staff.email, role="admin", linked_user=staff)
        self.client.force_login(staff)
        with mock.patch('reviews.services.polar_billing.requests.post') as post:
            self.client.post(reverse('billing_checkout', args=['premium', 'month']))
        post.assert_not_called()


class StarterYearlyHiddenTests(TestCase):
    """The yearly Starter option stays hidden until its Polar product exists."""

    @override_settings(POLAR_ACCESS_TOKEN='t', POLAR_PRODUCT_STARTER_MONTHLY='prod-sm',
                       POLAR_PRODUCT_PREMIUM_MONTHLY='prod-pm', POLAR_PRODUCT_STARTER_YEARLY='')
    def test_only_monthly_prices_shown(self):
        owner, _ = make_owner()
        self.client.force_login(owner)
        html = self.client.get(reverse('billing')).content.decode()
        self.assertIn("CHF 19", html)
        self.assertIn("CHF 39", html)
        self.assertNotIn("billed yearly", html)


# ---------------------------------------------------------------- bug #7: scheduled jobs

from pathlib import Path

from django.conf import settings as dj_settings

from reviews.services import weekly_summary


def make_review_for(owner, rating=5, age_days=1, **kw):
    review = Review.objects.create(
        user=owner, business_name="Cafe Luna", reviewer_name="Ann", rating=rating,
        comment="Nice", status=kw.pop('status', 'pending'), **kw)
    Review.objects.filter(id=review.id).update(created_at=dj_tz.now() - _td(days=age_days))
    return review


class WeeklySummaryTests(TestCase):
    def setUp(self):
        self.owner, self.profile = make_owner()
        make_review_for(self.owner, rating=5, status='posted')
        make_review_for(self.owner, rating=1, status='pending', ai_draft_reply="Sorry!")

    def test_sends_one_email_with_the_numbers(self):
        self.assertEqual(weekly_summary.send_weekly_summaries(), 1)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("New reviews: 2", body)
        self.assertIn("Average rating: 3.0", body)
        self.assertIn("Replied: 1", body)
        self.assertIn("Drafts waiting for your approval: 1", body)
        self.assertEqual(mail.outbox[0].to, [self.owner.email])

    def test_running_twice_in_a_week_sends_once(self):
        weekly_summary.send_weekly_summaries()
        weekly_summary.send_weekly_summaries()
        self.assertEqual(len(mail.outbox), 1)

    def test_sends_again_next_week(self):
        weekly_summary.send_weekly_summaries()
        later = dj_tz.now() + _td(days=7)
        make_review_for(self.owner, age_days=0)
        self.assertEqual(weekly_summary.send_weekly_summaries(now=later), 1)

    def test_read_only_accounts_get_nothing(self):
        expire_trial(self.profile)
        self.assertEqual(weekly_summary.send_weekly_summaries(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_quiet_week_sends_nothing(self):
        Review.objects.all().delete()
        make_review_for(self.owner, age_days=10)
        self.assertEqual(weekly_summary.send_weekly_summaries(), 0)

    def test_simulated_reviews_are_not_counted(self):
        Review.objects.all().delete()
        make_review_for(self.owner, is_simulated=True)
        self.assertEqual(weekly_summary.send_weekly_summaries(), 0)

    def test_command_runs(self):
        call_command('send_weekly_summary')
        self.assertEqual(len(mail.outbox), 1)


class NightlyTrainingTests(TestCase):
    def setUp(self):
        self.owner, self.profile = make_owner()
        self.review = make_review_for(self.owner)
        EditLog.objects.create(user=self.owner, review=self.review, ai_draft="Hi", final_text="Hello there")

    def run_training(self, **kw):
        from reviews.tasks import analyze_edit_patterns
        with mock.patch('reviews.services.ai_responder.summarize_edit_patterns',
                        return_value="Prefers warm, short replies") as summarize:
            analyze_edit_patterns(**kw)
        return summarize

    def test_learns_from_new_edits_and_remembers_when(self):
        summarize = self.run_training()
        summarize.assert_called_once()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.learned_patterns, "Prefers warm, short replies")
        self.assertIsNotNone(self.profile.last_training_run)

    def test_second_night_without_new_edits_costs_nothing(self):
        self.run_training()
        self.assertEqual(self.run_training().call_count, 0)

    def test_a_new_edit_triggers_training_again(self):
        self.run_training()
        EditLog.objects.create(user=self.owner, review=self.review, ai_draft="A", final_text="B")
        self.assertEqual(self.run_training().call_count, 1)

    def test_manual_run_now_always_runs(self):
        self.run_training()
        self.assertEqual(self.run_training(user_id=self.owner.id).call_count, 1)

    def test_read_only_accounts_are_skipped(self):
        expire_trial(self.profile)
        self.assertEqual(self.run_training().call_count, 0)

    def test_command_runs(self):
        with mock.patch('reviews.services.ai_responder.summarize_edit_patterns', return_value="x") as s:
            call_command('run_nightly_training')
        s.assert_called_once()


class WorkflowScheduleTests(TestCase):
    def test_github_workflow_schedules_every_job(self):
        text = (Path(dj_settings.BASE_DIR) / '.github' / 'workflows' / 'sync.yml').read_text(encoding='utf-8')
        for cron in ('0 2 * * *', '5 5-20 * * *', '30 3 * * *', '0 7 * * 1'):
            self.assertIn(f'cron: "{cron}"', text)
            self.assertIn(f"github.event.schedule == '{cron}'", text)
        for command in ('sync_reviews', 'send_due_alerts', 'run_nightly_training', 'send_weekly_summary'):
            self.assertIn(f'manage.py {command}', text)

    def test_run_workflow_button_can_start_every_job(self):
        text = (Path(dj_settings.BASE_DIR) / '.github' / 'workflows' / 'sync.yml').read_text(encoding='utf-8')
        self.assertIn('type: choice', text)
        for job in ('sync', 'due-alerts', 'nightly-training', 'weekly-summary'):
            self.assertIn(f'- {job}', text)
            self.assertIn(f"inputs.job == '{job}'", text)
