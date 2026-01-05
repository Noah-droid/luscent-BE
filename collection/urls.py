from django.urls import path
from .views import (
    CollectionListCreateView, 
    CollectionDetailView, 
    SwaggerImportView,
    CrawlerImportView
)

urlpatterns = [
    path("projects/<uuid:project_id>/", CollectionListCreateView.as_view(), name="collection-list-create"),
    path("<int:pk>/", CollectionDetailView.as_view(), name="collection-detail"),
    path("projects/<int:project_id>/import-swagger/", SwaggerImportView.as_view(), name="swagger-import"),
    path("projects/<int:project_id>/import-crawler/", CrawlerImportView.as_view(), name="crawler-import"),
]

