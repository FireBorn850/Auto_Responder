"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),  # Handles Google OAuth login & callback routes

    # Django's built-in language-switcher endpoint (POST here to change
    # the active language, stored in session + cookie). Deliberately NOT
    # wrapped in i18n_patterns() / URL-prefixed — this app has public
    # URLs (QR code redirects scanned from print, webhook endpoints
    # called by external systems) that must never change shape based on
    # language, so language selection is cookie/session-based instead.
    path('i18n/', include('django.conf.urls.i18n')),

    path('', include('reviews.urls')),          # Main dashboard & reviews views
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)