from django.contrib import admin
from .models import Review

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('reviewer_name', 'rating', 'detected_language', 'status', 'created_at')
    list_filter = ('rating', 'detected_language', 'status')
    search_fields = ('reviewer_name', 'comment', 'ai_draft_reply')