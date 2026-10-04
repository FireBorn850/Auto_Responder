from django.core.management.base import BaseCommand

from reviews.services.weekly_summary import send_weekly_summaries


class Command(BaseCommand):
    help = "Sends the weekly summary email to every active business owner (run weekly by GitHub Actions)."

    def handle(self, *args, **options):
        sent = send_weekly_summaries()
        self.stdout.write(self.style.SUCCESS(f"Weekly summaries sent: {sent}"))
