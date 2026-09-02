from django.contrib import admin
from .models import Review
from .models import AccessCode

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('reviewer_name', 'rating', 'detected_language', 'status', 'created_at')
    list_filter = ('rating', 'detected_language', 'status')
    search_fields = ('reviewer_name', 'comment', 'ai_draft_reply')


@admin.register(AccessCode)
class AccessCodeAdmin(admin.ModelAdmin):
    list_display = ['code', 'business_name', 'redeemed_by', 'redeemed_at', 'expires_at', 'granted_linkedin_recommendation', 'granted_case_study_permission']
    list_filter = ['granted_linkedin_recommendation', 'granted_case_study_permission']
    search_fields = ['code', 'business_name']