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
    path('team-access/invite/delete/<int:invite_id>/', views.delete_invite_view, name='delete_invite'),
    path('notifications/', views.simulator_page_view, name='review_simulator'),
    path('notifications/review/<int:review_id>/delete/', views.delete_simulated_review_view, name='delete_simulated_review'),
    path('notifications/review/<int:review_id>/regenerate/', views.regenerate_simulated_review_view, name='regenerate_simulated_review'),
    path('account/settings/', views.account_settings_view, name='account_settings'),
    path('notifications/clear/', views.clear_simulation_history_view, name='clear_simulation_history'),
    path('notifications/export/', views.export_simulated_reviews_csv_view, name='export_simulated_csv'),
    path('privacy-policy/', views.privacy_policy_view, name='privacy_policy'),
    path('terms-of-service/', views.terms_of_service_view, name='terms_of_service'),
    path('getting-started/', views.getting_started_view, name='getting_started'),

    # ==========================================
    # 2. REVIEW & SETTINGS ACTIONS
    # ==========================================
    path('sync-google-reviews/', views.sync_google_reviews_view, name='sync_google_reviews'),
    path('sync-tripadvisor-reviews/', views.sync_tripadvisor_reviews_view, name='sync_tripadvisor_reviews'),
    path('sync-frequency/update/', views.update_sync_frequency_view, name='update_sync_frequency'),
    path('trustpilot/waitlist/', views.join_trustpilot_waitlist_view, name='join_trustpilot_waitlist'),
    path('webhook/rotate-key/', views.regenerate_webhook_token_view, name='regenerate_webhook_token'),
    path('integrations/request/', views.request_integration_view, name='request_integration'),
    path('export-csv/', views.export_reviews_csv_view, name='export_csv'),
    path('export-insights/', views.export_insights_report_view, name='export_insights_report'),
    path('dashboard/insights/', views.dashboard_insights_view, name='dashboard_insights'),
    path('settings/update/', views.update_settings_view, name='update_settings'),
    path('settings/preview/', views.preview_ai_response_view, name='preview_ai_response'),
    path('generate/<int:review_id>/', views.generate_draft_view, name='generate_draft'),
    path('approve/<int:review_id>/', views.approve_review_view, name='approve_review'),
    path('settings/train/', views.run_ai_training_view, name='run_ai_training'),
    path('reviews/add/', views.add_review_view, name='add_review'),  # Simulation Submit Handler
    path('account/session/revoke/<str:session_key>/', views.revoke_session_view, name='revoke_session'),
    path('demo/preview/', views.public_demo_preview_view, name='public_demo_preview'),
    path('redeem/', views.redeem_access_code_view, name='redeem_access_code'),
    path('request-access/', views.request_access_code_view, name='request_access_code'),

    # ==========================================
    # 3. TEAM / COMPETITOR ACTIONS
    # ==========================================
    path('competitor/add/', views.add_competitor_view, name='add_competitor'),
    path('competitor/delete/<int:competitor_id>/', views.delete_competitor_view, name='delete_competitor'),

    # ==========================================
    # 4. SMART QR CODE ROUTER & ACTIONS
    # ==========================================
    path('qr/create/', views.create_qr_view, name='create_qr'),
    path('qr/delete/<int:qr_id>/', views.delete_qr_view, name='delete_qr'),
    path('qr/<slug:slug>/', views.qr_redirect_view, name='qr_redirect'),  # Public redirection endpoint
    path('qr-image/<slug:slug>.png', views.qr_image_view, name='qr_image'),
    path('qr-print/<slug:slug>/', views.qr_print_template_view, name='qr_print_template'),

    # ==========================================
    # 5. API WEBHOOKS
    # ==========================================
    path('api/webhook/google-review/<uuid:token>/', views_api.google_review_webhook, name='google_review_webhook'),
]
