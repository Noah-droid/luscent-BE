from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, APIToken


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Custom user admin"""
    list_display = ['email', 'username', 'is_staff', 'test_runs_count', 'token_balance', 'created_at']
    list_filter = ['is_staff', 'is_active', 'created_at']
    search_fields = ['email', 'username']
    ordering = ['-created_at']
    
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Usage Tracking', {
            'fields': ('test_runs_count', 'token_balance', 'is_verified', 'verification_token')
        }),
    )


@admin.register(APIToken)
class APITokenAdmin(admin.ModelAdmin):
    """API Token admin"""
    list_display = ['user', 'name', 'token_preview', 'is_active', 'created_at', 'last_used_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['user__email', 'name', 'token']
    readonly_fields = ['token', 'created_at', 'last_used_at']
    ordering = ['-created_at']
    
    def token_preview(self, obj):
        """Show first 20 characters of token"""
        return f"{obj.token[:20]}..."
    token_preview.short_description = 'Token'


