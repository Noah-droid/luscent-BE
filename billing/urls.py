from django.urls import path
from .views import UsageSummaryView, TransactionListView

urlpatterns = [
    path('usage/', UsageSummaryView.as_view(), name='usage-summary'),
    path('transactions/', TransactionListView.as_view(), name='transaction-list'),
]
