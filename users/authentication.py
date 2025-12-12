from rest_framework import authentication, exceptions
from django.utils import timezone
from .models import APIToken


class TokenAuthentication(authentication.BaseAuthentication):
    """
    Token authentication for CLI
    """
    
    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        
        if not auth_header.startswith('Bearer '):
            return None
        
        token = auth_header.replace('Bearer ', '').strip()
        
        if not token:
            return None
        
        try:
            api_token = APIToken.objects.select_related('user').get(
                token=token,
                is_active=True
            )
        except APIToken.DoesNotExist:
            raise exceptions.AuthenticationFailed('Invalid token')
        
        # Update last used timestamp
        api_token.last_used_at = timezone.now()
        api_token.save(update_fields=['last_used_at'])
        
        return (api_token.user, api_token)
    
    def authenticate_header(self, request):
        return 'Bearer'

