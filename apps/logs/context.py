import threading

_thread_locals = threading.local()

def set_context(user=None, project=None):
    """Set logging context manually"""
    _thread_locals.user = user
    _thread_locals.project = project

def get_context():
    """Get current context"""
    user = getattr(_thread_locals, 'user', None)
    project = getattr(_thread_locals, 'project', None)
    
    # Try to auto-detect from Django request if not set
    if not user or not project:
        try:
            from django.utils import timezone
            from django.contrib.auth.models import User
            request = getattr(_thread_locals, 'request', None)
            
            if request and hasattr(request, 'user') and request.user.is_authenticated:
                if not user:
                    user = request.user
                
                if not project:
                    from ..project.models import UserCurrentProject
                    try:
                        user_current_project = UserCurrentProject.objects.get(user=request.user)
                        project = user_current_project.project
                    except:
                        pass
        except:
            pass
    
    return user, project

# Middleware to capture request context
class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _thread_locals.request = request
        response = self.get_response(request)
        if hasattr(_thread_locals, 'request'):
            del _thread_locals.request
        return response
