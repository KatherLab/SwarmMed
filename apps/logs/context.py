"""
Logging Context Management.
Uses thread-local storage to track the current user and project context,
making it easier to associate log entries with specific users or projects
without passing objects through every function call.
"""

import threading

# Thread-local storage to keep track of context within a single request/thread
_thread_locals = threading.local()


def set_context(user=None, project=None):
    """
    Manually set the logging context for the current thread.
    Useful for background tasks (like Celery) where request middleware isn't active.
    """
    _thread_locals.user = user
    _thread_locals.project = project


def get_context():
    """
    Retrieves the current user and project from thread-local storage.
    If not explicitly set, it attempts to auto-detect from the Django request.

    Returns:
        tuple: (User object or None, Project object or None)
    """
    user = getattr(_thread_locals, 'user', None)
    project = getattr(_thread_locals, 'project', None)

    # If context isn't set, try to extract it from the active request
    if not user or not project:
        try:
            request = getattr(_thread_locals, 'request', None)

            if request and hasattr(request,
                                   'user') and request.user.is_authenticated:
                if not user:
                    user = request.user

                if not project:
                    # Attempt to find the user's currently active project
                    from ..project.models import UserCurrentProject
                    try:
                        user_current_project = UserCurrentProject.objects.get(
                            user=request.user
                        )
                        project = user_current_project.project
                    except UserCurrentProject.DoesNotExist:
                        pass
        except Exception:
            # We fail silently here to ensure logging never crashes the
            # application
            pass

    return user, project


class RequestContextMiddleware:
    """
    Middleware that captures the current request object into thread-local storage.
    This allows the logger to automatically know which user is performing an action.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Store request at the beginning of the request-response cycle
        _thread_locals.request = request

        response = self.get_response(request)

        # Clean up after the request is finished to prevent memory leaks
        if hasattr(_thread_locals, 'request'):
            del _thread_locals.request

        return response
