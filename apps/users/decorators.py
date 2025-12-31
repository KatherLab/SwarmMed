"""
Custom decorators for access control in the users application.
Provides simple checks to ensure users have the required roles (admin/developer)
before they can access specific view functions.
"""

from django.core.exceptions import PermissionDenied


def admin_required(view_func):
    """
    Decorator that restricts access to the view only to users with the 'admin' role.
    Raises a PermissionDenied exception if the requirement is not met.
    """
    def _wrapped_view(request, *args, **kwargs):
        # 1. User must be logged in.
        # 2. Allow superusers automatically.
        # 3. Otherwise, check if user has a profile and the 'admin' role.
        if not request.user.is_authenticated:
            raise PermissionDenied

        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        has_profile = hasattr(request.user, 'profile')
        if not has_profile or request.user.profile.role != 'admin':
            raise PermissionDenied

        return view_func(request, *args, **kwargs)

    return _wrapped_view


def developer_required(view_func):
    """
    Decorator that restricts access to users with either 'admin' or 'developer' roles.
    Allows developers and admins to access shared workspace features.
    """
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            raise PermissionDenied

        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        has_profile = hasattr(request.user, 'profile')
        if not has_profile or \
                request.user.profile.role not in ['admin', 'developer']:
            raise PermissionDenied

        return view_func(request, *args, **kwargs)

    return _wrapped_view
