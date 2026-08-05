from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.utils import timezone
from datetime import timedelta
from reviews.models import Review


class Command(BaseCommand):
    help = "Sends weekly analytics summary emails to business owners."

    def handle(self, *args, **kwargs):
        now = timezone.now()
        one_week_ago = now - timedelta(days=7)

        users = User.objects.all()
        sent_count = 0

        for user in users:
            # Skip users without an email address
            if not user.email:
                continue

            # Calculate analytics for the past 7 days
            weekly_reviews = Review.objects.filter(user=user, created_at__gte=one_week_ago)
            total_reviews = weekly_reviews.count()

            if total_reviews == 0:
                continue  # Skip sending email if there were no reviews this week

            # Calculate average rating
            total_rating = sum(review.rating for review in weekly_reviews)
            avg_rating = round(total_rating / total_reviews, 1)

            # Auto-responded count
            auto_responded = weekly_reviews.filter(status='approved').count()

            subject = "📊 Your Weekly Review Booster Summary"
            message = (
                f"Hello {user.username},\n\n"
                f"Here is your weekly performance summary for the past 7 days:\n\n"
                f"• Total Reviews Received: {total_reviews}\n"
                f"• Average Rating: {avg_rating} / 5.0 ★\n"
                f"• AI Auto-Responses Handled: {auto_responded}\n\n"
                f"Log in to your dashboard to view detailed feedback and response drafts.\n\n"
                f"Best regards,\n"
                f"The Review Booster Team"
            )

            send_mail(
                subject=subject,
                message=message,
                from_email=None,  # Uses DEFAULT_FROM_EMAIL from settings.py
                recipient_list=[user.email],
                fail_silently=False,
            )
            sent_count += 1

        self.stdout.write(
            self.style.SUCCESS(f"Successfully processed weekly summaries. Sent {sent_count} email(s).")
        )