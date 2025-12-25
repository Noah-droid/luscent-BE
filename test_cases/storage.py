import os
import logging
import cloudinary
import cloudinary.uploader
import cloudinary.api
from django.conf import settings

logger = logging.getLogger(__name__)


class CloudinaryStorage:
    """Handles file uploads to Cloudinary with quota management."""
    
    def __init__(self):
        """Initialize Cloudinary configuration."""
        cloudinary.config(
            cloud_name=getattr(settings, 'CLOUDINARY_CLOUD_NAME', ''),
            api_key=getattr(settings, 'CLOUDINARY_API_KEY', ''),
            api_secret=getattr(settings, 'CLOUDINARY_API_SECRET', ''),
            secure=True
        )
    
    def upload_screenshot(self, file_path, project, test_run_id):
        """
        Upload a screenshot to Cloudinary.
        
        Args:
            file_path: Path to the screenshot file
            project: Project instance (for quota checking)
            test_run_id: ID of the test run
        
        Returns:
            dict: {
                'url': 'https://res.cloudinary.com/...',
                'size_bytes': 12345,
                'public_id': 'qai/user_1/run_123'
            }
        
        Raises:
            ValueError: If quota exceeded or file doesn't exist
        """
        if not os.path.exists(file_path):
            raise ValueError(f"File not found: {file_path}")
        
        # Check file size
        file_size = os.path.getsize(file_path)
        
        # Check quota
        if not project.can_upload(file_size):
            raise ValueError(
                f"Storage quota exceeded. Used: {project.storage_used_mb:.2f} MB / "
                f"{project.storage_quota_mb:.2f} MB"
            )
        
        # Upload to Cloudinary
        try:
            folder = f"qai/user_{project.user.id}/project_{project.id}"
            public_id = f"{folder}/run_{test_run_id}"
            
            result = cloudinary.uploader.upload(
                file_path,
                folder=folder,
                public_id=f"run_{test_run_id}",
                resource_type="image",
                overwrite=True,
                invalidate=True
            )
            
            # Track storage usage
            project.increment_storage(file_size)
            
            logger.info(f"Uploaded screenshot for run {test_run_id}: {result['secure_url']}")
            
            return {
                'url': result['secure_url'],
                'size_bytes': file_size,
                'public_id': result['public_id']
            }
            
        except Exception as e:
            logger.error(f"Cloudinary upload failed: {e}")
            raise ValueError(f"Upload failed: {str(e)}")
    
    def delete_screenshot(self, public_id, project, size_bytes):
        """
        Delete a screenshot from Cloudinary and update quota.
        
        Args:
            public_id: Cloudinary public ID
            project: Project instance
            size_bytes: Size of the file being deleted
        """
        try:
            cloudinary.uploader.destroy(public_id, resource_type="image")
            project.decrement_storage(size_bytes)
            logger.info(f"Deleted screenshot: {public_id}")
        except Exception as e:
            logger.error(f"Failed to delete screenshot {public_id}: {e}")
    
    def get_project_usage(self, project):
        """
        Get storage usage for a project from Cloudinary.
        
        Args:
            project: Project instance
        
        Returns:
            dict: {'total_bytes': 12345, 'file_count': 10}
        """
        try:
            folder = f"qai/user_{project.user.id}/project_{project.id}"
            
            result = cloudinary.api.resources(
                type="upload",
                prefix=folder,
                max_results=500
            )
            
            total_bytes = sum(r.get('bytes', 0) for r in result.get('resources', []))
            file_count = len(result.get('resources', []))
            
            return {
                'total_bytes': total_bytes,
                'file_count': file_count
            }
        except Exception as e:
            logger.error(f"Failed to get usage for project {project.id}: {e}")
            return {'total_bytes': 0, 'file_count': 0}


