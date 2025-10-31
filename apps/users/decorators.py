from django.core.exceptions import PermissionDenied

def admin_required(view_func):
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated or request.user.profile.role != 'admin':
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped_view

def developer_required(view_func):
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated or request.user.profile.role not in ['admin', 'developer']:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped_view