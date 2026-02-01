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
    path("<int:pk>/", CollectionDetailView.as_view(), name="collection-detail"),
    
    # Endpoint management within collections
    path("<int:collection_id>/endpoints/", EndpointListCreateView.as_view(), name="endpoint-list-create"),
    path("endpoints/<int:pk>/", EndpointDetailView.as_view(), name="endpoint-detail"),
    
    # Bulk Imports
    path("projects/<uuid:project_id>/import-swagger/", SwaggerImportView.as_view(), name="swagger-import"),
    path("projects/<uuid:project_id>/import-crawler/", CrawlerImportView.as_view(), name="crawler-import"),
]
