import logging
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from .models import Project
from .serializers import ProjectSerializer

logger = logging.getLogger(__name__)


class ProjectListCreateView(generics.ListCreateAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Project.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        project = serializer.save(user=self.request.user)
        
        # Auto-create collection and import swagger if swagger_url provided
        swagger_url = self.request.data.get('swagger_url')
        if swagger_url:
            self._auto_import_swagger(project, swagger_url)
    
    def _auto_import_swagger(self, project, swagger_url):
        """
        Create a collection for the project and queue the Swagger import in the
        background so project creation never blocks on fetching/parsing a spec.
        Progress/errors surface through the import job (ImportJob), the same as
        collection-level imports.
        """
        try:
            from collection.models import Collection
            from collection.importer import queue_swagger_import

            # Create collection immediately (empty) so the project is usable
            collection = Collection.objects.create(
                project=project,
                name=f"{project.name} API",
                source='swagger',
                base_url=project.target_url or '',
                description=f"Auto-imported from {swagger_url}",
            )

            queue_swagger_import(
                collection,
                source="url",
                spec_name=swagger_url,
                skip_validation=False,
            )
            logger.info(f"[Project] Queued swagger import for project {project.name} from {swagger_url}")

        except Exception as e:
            logger.error(f"[Project] Failed to queue auto-import for project {project.name}: {e}")


class ProjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Project.objects.filter(user=self.request.user)




