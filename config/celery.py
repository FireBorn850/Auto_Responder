import os
from celery import Celery
from celery.schedules import crontab

# Set default Django settings module for Celery
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')

# Load configuration using the 'CELERY_' prefix in settings.py
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in tasks.py files across installed Django apps
app.autodiscover_tasks()


app.conf.beat_schedule = {
    'nightly-ai-training': {
        'task': 'reviews.tasks.analyze_edit_patterns',
        'schedule': crontab(hour=3, minute=0),  # 3:00 AM server time, every day
    },
}

