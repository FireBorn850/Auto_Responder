from django.contrib import admin
from .models import Review
from .models import AccessCode
from django.core.mail import send_mail

@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('reviewer_name', 'rating', 'detected_language', 'status', 'created_at')
    list_filter = ('rating', 'detected_language', 'status')
    search_fields = ('reviewer_name', 'comment', 'ai_draft_reply')


@admin.register(AccessCode)
class AccessCodeAdmin(admin.ModelAdmin):
    list_display = ['code', 'business_name', 'requested_email', 'status', 'redeemed_by', 'redeemed_at', 'expires_at', 'created_at']
    list_filter = ['status', 'granted_linkedin_recommendation', 'granted_case_study_permission']
    search_fields = ['code', 'business_name', 'requested_email']
    actions = ['approve_and_send']

    def approve_and_send(self, request, queryset):
        sent = 0
        for access_code in queryset.filter(status='pending'):
            try:
                send_mail(
                    subject="Your Mehrly Founding Partner code",
                    message=(
                        f"Hi,\n\nHere is your Founding Partner access code:\n\n"
                        f"{access_code.code}\n\n"
                        f"1. Sign up: {request.build_absolute_uri('/accounts/signup/')}\n"
                        f"2. Redeem it: {request.build_absolute_uri('/redeem/')}\n\n"
                        f"— Mehrly"
                    ),
                    from_email=None,
                    recipient_list=[access_code.requested_email],
                    fail_silently=False,
                )
                access_code.status = 'approved'
                access_code.save(update_fields=['status'])
                sent += 1
            except Exception:
                pass
        self.message_user(request, f"Sent {sent} code(s).")
    approve_and_send.short_description = "Approve & email selected codes"