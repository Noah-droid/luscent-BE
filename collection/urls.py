from django.urls import path
from .views import (
    CollectionListCreateView,
    CollectionDetailView,
    EndpointListCreateView,
    EndpointDetailView,
    SwaggerImportView,
    CrawlerImportView
)

urlpatterns = [
    # Collection management
    path("projects/<uuid:project_id>/", CollectionListCreateView.as_view(), name="collection-list-create"),
    path("<uuid:pk>/", CollectionDetailView.as_view(), name="collection-detail"),
    
    # Endpoint management within collections
    path("<uuid:collection_id>/endpoints/", EndpointListCreateView.as_view(), name="endpoint-list-create"),
    path("endpoints/<int:pk>/", EndpointDetailView.as_view(), name="endpoint-detail"),
    
    # Bulk Imports
    path("<uuid:collection_id>/import-swagger/", SwaggerImportView.as_view(), name="swagger-import"),
    path("<uuid:collection_id>/import-crawler/", CrawlerImportView.as_view(), name="crawler-import"),
]
