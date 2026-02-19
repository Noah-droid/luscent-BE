from django.contrib import admin
from .models import TestCase, TestRun

@admin.register(TestCase)
class TestCaseAdmin(admin.ModelAdmin):
    list_display = ('name', 'endpoint', 'runner_type', 'priority', 'keep_alive')
    list_filter = ('runner_type', 'priority', 'keep_alive', 'category')
    search_fields = ('name', 'description')

@admin.register(TestRun)
class TestRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'test_case', 'status', 'response_status', 'response_time_ms', 'executed_at')
    list_filter = ('status', 'triggered_by')
    readonly_fields = ('sandbox_id', 'extracted_data', 'logs')
    search_fields = ('test_case__name', 'sandbox_id', 'batch_id')
