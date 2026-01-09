from rest_framework import serializers
from django.contrib.auth import authenticate
from .models import User, APIToken


class UserSerializer(serializers.ModelSerializer):
    """User serializer"""
    
    class Meta:
        model = User
        fields = ['id', 'email', 'username', 'created_at', ]
        read_only_fields = ['id', 'created_at',]


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
    
    def create(self, validated_data):
        validated_data.pop('confirm_password')
        user = User.objects.create_user(
            email=validated_data['email'],
            username=validated_data['email'].split('@')[0],
            password=validated_data['password']
        )
        return user


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
