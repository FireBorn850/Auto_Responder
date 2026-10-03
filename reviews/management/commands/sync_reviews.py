from django.core.management.base import BaseCommand
from reviews.tasks import poll_google_reviews


class Command(BaseCommand):
    help = "Auto-sync Google reviews for every business whose sync is due"

    def handle(self, *args, **options):
        from reviews.services.sync_jobs import finish_abandoned_jobs

        # Manual syncs whose owner closed the tab: finish them first (free —
        # the data was already paid for when the sync was started).
        finished = finish_abandoned_jobs()
        if finished:
            self.stdout.write(f"Finished {finished} abandoned manual sync(s).")
        poll_google_reviews()  # a shared_task can be called directly, it runs inline
