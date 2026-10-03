from django.core.management.base import BaseCommand

from reviews.tasks import send_due_alerts


class Command(BaseCommand):
    help = "Send negative-review alerts that were held back by quiet hours and are now due."

    def handle(self, *args, **options):
        sent = send_due_alerts()
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} due alert(s)."))
