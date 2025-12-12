from django.urls import path
from .views import (
    RegisterView,
    LoginView,
    CurrentUserView,
    UserTokensView,
    RefreshTokenView,
    LogoutView
)

app_name = 'users'

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('me/', CurrentUserView.as_view(), name='current-user'),
    path('tokens/', UserTokensView.as_view(), name='tokens'),
    path('token/refresh/', RefreshTokenView.as_view(), name='token-refresh'),
]
