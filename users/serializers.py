from rest_framework import serializers
from django.contrib.auth import authenticate
from .models import User, APIToken, TestCredential


class UserSerializer(serializers.ModelSerializer):
    """User serializer"""
    has_github_linked = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = ['id', 'email', 'username', 'full_name', 'organization_name', 'onboarding_completed', 'created_at', 'github_username', 'has_github_linked']
        read_only_fields = ['id', 'created_at']

    def get_has_github_linked(self, obj):
        return bool(obj.github_token)


class RegisterSerializer(serializers.ModelSerializer):
    """User registration serializer"""
    password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)
    
    class Meta:
        model = User
        fields = ['email', 'password', 'confirm_password']
    
    def validate(self, data):
        if data['password'] != data['confirm_password']:
            raise serializers.ValidationError({"password": "Passwords do not match"})
        return data


class TestCredentialSerializer(serializers.ModelSerializer):
    project_name = serializers.ReadOnlyField(source='project.name', default=None)
    scope_label = serializers.ReadOnlyField()

    class Meta:
        model = TestCredential
        fields = ['id', 'provider', 'email', 'password', 'metadata', 'description', 'is_active', 'project', 'project_name', 'scope_label', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']
        extra_kwargs = {
            'password': {'write_only': True, 'required': False},
            'email': {'required': False},
            'project': {'required': False, 'allow_null': True},
        }
    
    def create(self, validated_data):
        validated_data.pop('confirm_password')
        user = User.objects.create_user(
            email=validated_data['email'],
            username=validated_data['email'].split('@')[0],
            password=validated_data['password']
        )
        return user


class OnboardingSerializer(serializers.Serializer):
    """Serializer for the onboarding step"""
    full_name = serializers.CharField(max_length=255)
    organization_name = serializers.CharField(max_length=255)



class LoginSerializer(serializers.Serializer):
    """User login serializer"""
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    
    def validate(self, data):
        user = authenticate(username=data['email'], password=data['password'])
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        if not user.is_active:
            raise serializers.ValidationError("User account is disabled")
        data['user'] = user
        return data


class APITokenSerializer(serializers.ModelSerializer):
    """API Token serializer"""
    
    class Meta:
        model = APIToken
        fields = ['id', 'token', 'name', 'created_at', 'last_used_at', 'is_active']
        read_only_fields = ['id', 'token', 'created_at', 'last_used_at']


class GithubLoginSerializer(serializers.Serializer):
    """Serializer for Github Login"""
    code = serializers.CharField(required=True)


class ForgotPasswordSerializer(serializers.Serializer):
    """Serializer for forgot password"""
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    """Serializer for reset password"""
    email = serializers.EmailField()
    otp = serializers.CharField(max_length=6)
    password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, data):
        if data['password'] != data['confirm_password']:
            raise serializers.ValidationError({"password": "Passwords do not match"})
        return data


class TestCredentialSerializer(serializers.ModelSerializer):
    project_name = serializers.ReadOnlyField(source='project.name', default=None)
    scope_label = serializers.ReadOnlyField()

    class Meta:
        model = TestCredential
        fields = ['id', 'provider', 'email', 'password', 'metadata', 'description', 'is_active', 'project', 'project_name', 'scope_label', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']
        extra_kwargs = {
            'password': {'write_only': True, 'required': False},
            'email': {'required': False},
            'project': {'required': False, 'allow_null': True},
        }
