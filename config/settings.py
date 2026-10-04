import os
from pathlib import Path
import dj_database_url
from dotenv import load_dotenv
from celery.schedules import crontab

# Load environment variables from .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Security Settings
SECRET_KEY = os.environ.get('SECRET_KEY')
DEBUG = os.environ.get('DEBUG', 'False').lower() == 'true'

# How many proxies in front of the app add themselves to X-Forwarded-For.
# Render = 1. Used to read the visitor's real IP for rate limits
# (reviews/services/client_ip.py). Local development = 0.
TRUSTED_PROXY_COUNT = int(os.environ.get('TRUSTED_PROXY_COUNT', '0' if DEBUG else '1'))
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-local-dev-only-key'
    else:
        raise Exception("SECRET_KEY environment variable is not set. Refusing to start in production without it.")


ALLOWED_HOSTS = [h.strip() for h in os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]

# Forms posted from these https origins pass the CSRF check (needed behind
# Render's proxy). Built from ALLOWED_HOSTS; override with CSRF_TRUSTED_ORIGINS.
_LOCAL_HOSTS = {'localhost', '127.0.0.1', '[::1]'}
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()] or [
    f"https://*{h}" if h.startswith('.') else f"https://{h}"
    for h in ALLOWED_HOSTS if h not in _LOCAL_HOSTS and h != '*'
]

# ------------------------------------------------------------------
# Production hardening (everything below is skipped when DEBUG=True,
# so local development on http://localhost keeps working).
# ------------------------------------------------------------------
if not DEBUG:
    # Render terminates HTTPS and tells us via this header. Without it
    # Django thinks every request is plain http.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # Render already redirects http -> https at its edge; turn this on too
    # with SECURE_SSL_REDIRECT=true if you ever move hosts.
    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'false').lower() == 'true'
    # Cookies (login session, CSRF) only ever travel over https.
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Browsers remember "this site is https-only" (HSTS). Starts at 1 year,
    # main domain only (subdomains like send.mehrly.com are email records).
    SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS') or 60 * 60 * 24 * 365)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get('SECURE_HSTS_INCLUDE_SUBDOMAINS', 'false').lower() == 'true'
    SECURE_HSTS_PRELOAD = False
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'
SESSION_COOKIE_HTTPONLY = True

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Required for django-allauth
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    
    # Local apps
    'anymail',
    'reviews',
]

SITE_ID = 1

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    
    # allauth account middleware
    'allauth.account.middleware.AccountMiddleware',
]

AUTHENTICATION_BACKENDS = (
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
)

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.i18n',
                'reviews.context_processors.billing_status',
                'reviews.context_processors.site_contact',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database
DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}
# Required because Neon's pooled endpoint uses PgBouncer
DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'] = True

# Shared cache in the database (free — a table in Neon). Rate limits and
# locks need ONE place every server process sees; the default in-memory
# cache is separate per process, so limits could be bypassed.
# The table is created by `python manage.py createcachetable` (build.sh).
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'django_cache',
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ('en', 'English'),
    ('fr', 'Français'),
    ('de', 'Deutsch'),
]

LOCALE_PATHS = [
    BASE_DIR / 'locale',
]

# Static files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# A reference to a static file that doesn't exist must not crash a page
# (it falls back to the plain file name instead of a 500 error).
WHITENOISE_MANIFEST_STRICT = False

# Django 5+ ignores the old STATICFILES_STORAGE setting, so WhiteNoise's
# compression + cache-busting file names were silently off in production.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage' if DEBUG
        else 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'


# Media files (user-uploaded content, e.g. business logos)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'


# Email Configuration
EMAIL_BACKEND = 'anymail.backends.resend.EmailBackend'
ANYMAIL = {
    'RESEND_API_KEY': os.environ.get('RESEND_API_KEY'),
}
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'Mehrly <noreply@mehrly.com>')

# Public contact shown on the site (support@ forwards to the owner's inbox via
# Cloudflare Email Routing) and where internal notifications (founder-code
# requests, integration requests) are sent. No personal address in the code.
SUPPORT_EMAIL = os.environ.get('SUPPORT_EMAIL', 'support@mehrly.com')
ADMIN_NOTIFY_EMAIL = os.environ.get('ADMIN_NOTIFY_EMAIL', SUPPORT_EMAIL)

# ==========================================
# Demo Mode — OFF unless explicitly switched on (DEMO_MODE=true).
# A demo switch must never be on by default in production.
# ==========================================
DEMO_MODE = os.environ.get('DEMO_MODE', 'False').lower() == 'true'

# ==========================================
# Google API Keys & Integrations
# ==========================================
GOOGLE_PLACES_API_KEY = os.environ.get('GOOGLE_PLACES_API_KEY', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')


# ==========================================
# Billing — Polar (Merchant of Record). See reviews/services/polar_billing.py
# Use POLAR_SERVER=sandbox while testing, production when live.
# ==========================================
POLAR_SERVER = os.environ.get('POLAR_SERVER', 'sandbox')
POLAR_ACCESS_TOKEN = os.environ.get('POLAR_ACCESS_TOKEN', '')
POLAR_WEBHOOK_SECRET = os.environ.get('POLAR_WEBHOOK_SECRET', '')
POLAR_PRODUCT_STARTER_MONTHLY = os.environ.get('POLAR_PRODUCT_STARTER_MONTHLY', '')
POLAR_PRODUCT_STARTER_YEARLY = os.environ.get('POLAR_PRODUCT_STARTER_YEARLY', '')
POLAR_PRODUCT_PREMIUM_MONTHLY = os.environ.get('POLAR_PRODUCT_PREMIUM_MONTHLY', '')
POLAR_PRODUCT_PREMIUM_YEARLY = os.environ.get('POLAR_PRODUCT_PREMIUM_YEARLY', '')

# ==========================================
# django-allauth & Google OAuth Setup
# ==========================================
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'APP': {
            'client_id': os.environ.get('GOOGLE_CLIENT_ID'),
            'secret': os.environ.get('GOOGLE_CLIENT_SECRET'),
            'key': ''
        },
        'SCOPE': [
            'profile',
            'email',
        ],
        'AUTH_PARAMS': {
            'access_type': 'online',
        }
    }
}

SOCIALACCOUNT_STORE_TOKENS = True
SOCIALACCOUNT_LOGIN_ON_GET = True

# Account settings (new allauth API)
ACCOUNT_AUTHENTICATION_METHOD = 'username_email'
ACCOUNT_LOGIN_METHODS = ['email', 'username']  # New: replaces ACCOUNT_AUTHENTICATION_METHOD
ACCOUNT_SIGNUP_FIELDS = ['email*', 'username*', 'password1*', 'password2*']

ACCOUNT_EMAIL_VERIFICATION = 'none'  # Skip email verification for now
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = True
ACCOUNT_LOGOUT_ON_GET = False  # Show logout confirmation page
ACCOUNT_LOGOUT_REDIRECT_URL = '/'
ACCOUNT_SESSION_REMEMBER = True

SOCIALACCOUNT_STORE_TOKEN = True
SOCIALACCOUNT_LOGIN_ON_GET = True

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'
    

# ==========================================
# Celery & Redis Configuration
# ==========================================
CELERY_BROKER_URL = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json' 
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = True

# Transport options to force RESP2 compatibility with Redis 5.x
CELERY_REDIS_BACKEND_TRANSPORT_OPTIONS = {'protocol_version': 2}
CELERY_BROKER_TRANSPORT_OPTIONS = {'protocol_version': 2}

CELERY_BEAT_SCHEDULE = {
    'poll-google-reviews-every-5-min': {
        'task': 'reviews.tasks.poll_google_reviews',
        'schedule': crontab(minute=0),
    },
}


SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')

DATAFORSEO_LOGIN = os.environ.get('DATAFORSEO_LOGIN', '')
DATAFORSEO_PASSWORD = os.environ.get('DATAFORSEO_PASSWORD', '')
DATAFORSEO_BASE_URL = os.environ.get('DATAFORSEO_BASE_URL') or 'https://api.dataforseo.com/v3'
# Manual syncs no longer wait inside the web request, so they can use the
# cheaper normal queue (1). Set to 2 for faster but pricier high priority.
DATAFORSEO_MANUAL_PRIORITY = int(os.environ.get('DATAFORSEO_MANUAL_PRIORITY', '1'))
AUTO_DRAFT_MAX_PER_SYNC = int(os.environ.get('AUTO_DRAFT_MAX_PER_SYNC', '5'))


# ==========================================
# Google Business Profile (optional auto-posting)
# ==========================================
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
SITE_URL = os.environ.get('SITE_URL', 'http://127.0.0.1:8000')
TOKEN_ENCRYPTION_KEY = os.environ.get('TOKEN_ENCRYPTION_KEY', '')