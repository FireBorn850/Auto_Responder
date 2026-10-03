from django.core.management.base import BaseCommand
from reviews.tasks import poll_google_reviews


class Command(BaseCommand):
    help = "Auto-sync Google reviews for every business whose sync is due"

    def handle(self, *args, **options):
        poll_google_reviews()  # a shared_task can be called directly, it runs inline