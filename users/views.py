from rest_framework import status, generics, permissions
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework import exceptions
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from .models import User, APIToken
from .serializers import (
    UserSerializer,
    RegisterSerializer,
    LoginSerializer,
    APITokenSerializer
)
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi


class RegisterView(generics.CreateAPIView):
    """User registration endpoint"""
    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    
    @swagger_auto_schema(
        operation_description="Register a new user",
        responses={
            201: openapi.Response(
                description="User registered successfully",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'user': openapi.Schema(type=openapi.TYPE_OBJECT, properties={
                            'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                            'email': openapi.Schema(type=openapi.TYPE_STRING),
                            'username': openapi.Schema(type=openapi.TYPE_STRING),
                        }),
                        'api_token': openapi.Schema(type=openapi.TYPE_STRING),
                        'access_token': openapi.Schema(type=openapi.TYPE_STRING),
                        'refresh_token': openapi.Schema(type=openapi.TYPE_STRING),
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                    }
                )
            )
        }
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Generate OTP (6 digits)
        import secrets
        otp = f"{secrets.randbelow(1_000_000):06d}"
        user.verification_token = otp
        user.is_verified = False
        user.save()
        
        # Send Email
        from notifications.services import send_verification_email
        send_verification_email(user, otp)
        
        # Create API token for CLI usage
        api_token = APIToken.objects.create(user=user, name="Default Token")
        
        # Generate JWT tokens for web dashboard
        refresh = RefreshToken.for_user(user)
        
        return Response({
            'user': UserSerializer(user).data,
            'api_token': api_token.token,  
            'message': 'User registered successfully. Please check your email for the verification code.'
        }, status=status.HTTP_201_CREATED)


class VerifyEmailView(APIView):
    """Verify user email via OTP"""
    permission_classes = [permissions.AllowAny]
    
    @swagger_auto_schema(
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['email', 'otp'],
            properties={
                'email': openapi.Schema(type=openapi.TYPE_STRING),
                'otp': openapi.Schema(type=openapi.TYPE_STRING),
            }
        )
    )
    def post(self, request):
        email = request.data.get('email')
        otp = request.data.get('otp')
        
        if not email or not otp:
            return Response({'error': 'Email and OTP required'}, status=400)
            
        try:
            user = User.objects.get(email=email)
            
            if user.is_verified:
                return Response({'message': 'Already verified'}, status=200)
            
            if user.verification_token != otp:
                return Response({'error': 'Invalid OTP'}, status=400)
                
            user.is_verified = True
            user.verification_token = None  # Clear OTP after use
            user.save()
            
            return Response({'message': 'Email verified successfully'}, status=200)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=404)


class ResendOTPView(APIView):
    """Resend OTP to user"""
    permission_classes = [permissions.AllowAny]
    
    @swagger_auto_schema(
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['email'],
            properties={
                'email': openapi.Schema(type=openapi.TYPE_STRING)
            }
        )
    )
    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({'error': 'Email required'}, status=400)
            
        try:
            user = User.objects.get(email=email)
            if user.is_verified:
                return Response({'message': 'Already verified'}, status=200)
                
            # Generate new OTP
            import secrets
            otp = f"{secrets.randbelow(1_000_000):06d}"
            user.verification_token = otp
            user.save()
            
            # Send Email
            from notifications.services import send_verification_email
            send_verification_email(user, otp)
            
            return Response({'message': 'OTP resent successfully'}, status=200)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=404)


class LoginView(APIView):
    """User login endpoint"""
    permission_classes = [permissions.AllowAny]
    
    @swagger_auto_schema(
        operation_description="Login user and get tokens",
        request_body=LoginSerializer,
        responses={
            200: openapi.Response(
                description="Login successful",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'user': openapi.Schema(type=openapi.TYPE_OBJECT, properties={
                            'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                            'email': openapi.Schema(type=openapi.TYPE_STRING),
                            'username': openapi.Schema(type=openapi.TYPE_STRING),
                        }),
                        'api_token': openapi.Schema(type=openapi.TYPE_STRING),
                        'access_token': openapi.Schema(type=openapi.TYPE_STRING),
                        'refresh_token': openapi.Schema(type=openapi.TYPE_STRING),
                    }
                )
            ),
            400: "Invalid credentials"
        }
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        
        # Check verification
        if not user.is_verified:
            return Response(
                {'error': 'Email not verified. Please check your inbox.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get or create API token for CLI usage
        api_token, created = APIToken.objects.get_or_create(
            user=user,
            name="Default Token",
            defaults={'is_active': True}
        )
        
        # Generate JWT tokens for web dashboard
        refresh = RefreshToken.for_user(user)
        
        return Response({
            'message': 'Login successful',
            'user': UserSerializer(user).data,
            'api_token': api_token.token, 
            'access_token': str(refresh.access_token),  
            'refresh_token': str(refresh),  
 
        })


class CurrentUserView(generics.RetrieveAPIView):
    """Get current authenticated user"""
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_object(self):
        return self.request.user


class UserTokensView(generics.ListCreateAPIView):
    """List and create API tokens"""
    serializer_class = APITokenSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        return APIToken.objects.filter(user=self.request.user)
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class RefreshTokenView(APIView):
    """Refresh JWT access token using refresh token"""
    permission_classes = [permissions.AllowAny]
    
    @swagger_auto_schema(
        operation_description="Refresh access token",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['refresh_token'],
            properties={
                'refresh_token': openapi.Schema(type=openapi.TYPE_STRING)
            }
        ),
        responses={
            200: openapi.Response(
                description="Token refreshed",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'access_token': openapi.Schema(type=openapi.TYPE_STRING),
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                    }
                )
            ),
            400: "Refresh token required",
            401: "Invalid or expired refresh token"
        }
    )
    def post(self, request):
        refresh_token = request.data.get('refresh_token')
        
        if not refresh_token:
            return Response(
                {'error': 'Refresh token is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            refresh = RefreshToken(refresh_token)
            return Response({
                'access_token': str(refresh.access_token),
                'message': 'Token refreshed successfully'
            })
        except Exception as e:
            return Response(
                {'error': 'Invalid or expired refresh token'},
                status=status.HTTP_401_UNAUTHORIZED
            )


class LogoutView(APIView):
    """Logout and blacklist refresh token"""
    permission_classes = [permissions.IsAuthenticated]
    
    @swagger_auto_schema(
        operation_description="Logout user (blacklist refresh token)",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['refresh_token'],
            properties={
                'refresh_token': openapi.Schema(type=openapi.TYPE_STRING)
            }
        ),
        responses={
            200: openapi.Response(
                description="Logged out successfully",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                    }
                )
            ),
            400: "Invalid token"
        }
    )
    def post(self, request):
        try:
            refresh_token = request.data.get('refresh_token')
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
            
            return Response({
                'message': 'Logged out successfully'
            })
        except Exception as e:
            return Response(
                {'error': 'Invalid token'},
                status=status.HTTP_400_BAD_REQUEST
            )

