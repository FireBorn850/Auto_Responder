from django.core.management.base import BaseCommand

from reviews.tasks import analyze_edit_patterns


class Command(BaseCommand):
    help = (
        "AI Training for every business with new owner/team edits (run nightly by GitHub Actions). "
        "Skips read-only accounts and businesses with nothing new to learn from."
    )

    def handle(self, *args, **options):
        analyze_edit_patterns()   # a shared_task can be called directly, it runs inline
        self.stdout.write(self.style.SUCCESS("Nightly AI training finished."))
