from django.urls import path
from . import views
from . import views_api  # Import API views

urlpatterns = [
    # ==========================================
    # 1. PUBLIC & DASHBOARD PAGES
    # ==========================================
    path('', views.landing_page, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('settings/', views.settings_page_view, name='ai_settings'),
    path('qr-booster/', views.qr_booster_page_view, name='qr_booster'),
    path('integrations/', views.integrations_page_view, name='integrations'),
    path('team-access/', views.competitors_page_view, name='competitors'),
    path('notifications/', views.simulator_page_view, name='review_simulator'),

    # ==========================================
    # 2. REVIEW & SETTINGS ACTIONS
    # ==========================================
    path('sync-google-reviews/', views.sync_google_reviews_view, name='sync_google_reviews'),
    path('settings/update/', views.update_settings_view, name='update_settings'),
    path('generate/<int:review_id>/', views.generate_draft_view, name='generate_draft'),
    path('approve/<int:review_id>/', views.approve_review_view, name='approve_review'),
    path('reviews/add/', views.add_review_view, name='add_review'),  # Simulation Submit Handler

    # ==========================================
    # 3. TEAM / COMPETITOR ACTIONS
    # ==========================================
    path('competitor/add/', views.add_competitor_view, name='add_competitor'),
    path('competitor/delete/<int:competitor_id>/', views.delete_competitor_view, name='delete_competitor'),

    # ==========================================
    # 4. SMART QR CODE ROUTER & ACTIONS
    # ==========================================
    path('qr/create/', views.create_qr_view, name='create_qr'),
    path('qr/<slug:slug>/', views.qr_redirect_view, name='qr_redirect'),  # Public redirection endpoint

    # ==========================================
    # 5. API WEBHOOKS
    # ==========================================
    path('api/webhook/google-review/', views_api.google_review_webhook, name='google_review_webhook'),
]