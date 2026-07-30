from django.urls import path
from . import views
from . import views_api  # <--- Import new API views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('generate/<int:review_id>/', views.generate_draft_view, name='generate_draft'),
    path('approve/<int:review_id>/', views.approve_review_view, name='approve_review'),
    
    # Webhook path for incoming Google reviews
    path('api/webhook/google-review/', views_api.google_review_webhook, name='google_review_webhook'),
]