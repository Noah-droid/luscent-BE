from django.urls import path
from .views import (
    RegisterView,
    LoginView,
    CurrentUserView,
    UserTokensView,
    RefreshTokenView,
    LogoutView,
    VerifyEmailView,
    ResendOTPView,
    GithubLoginView,
    GithubLinkView,
    GithubReposView,
    ForgotPasswordView,
    ResetPasswordView,
    OnboardingView,
    TestCredentialListCreateView,
    TestCredentialDetailView
)

app_name = 'users'

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('verify-email/', VerifyEmailView.as_view(), name='verify-email'),
    path('resend-otp/', ResendOTPView.as_view(), name='resend-otp'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('me/', CurrentUserView.as_view(), name='current-user'),
    path('tokens/', UserTokensView.as_view(), name='tokens'),
    path('token/refresh/', RefreshTokenView.as_view(), name='token-refresh'),
    path('github/login/', GithubLoginView.as_view(), name='github-login'),
    path('github/link/', GithubLinkView.as_view(), name='github-link'),
    path('github/repos/', GithubReposView.as_view(), name='github-repos'),
    path('forgot-password/', ForgotPasswordView.as_view(), name='forgot-password'),
    path('reset-password/', ResetPasswordView.as_view(), name='reset-password'),
    path('onboarding/', OnboardingView.as_view(), name='onboarding'),
    path('credentials/', TestCredentialListCreateView.as_view(), name='credentials'),
    path('credentials/<int:pk>/', TestCredentialDetailView.as_view(), name='credential-detail'),
]
