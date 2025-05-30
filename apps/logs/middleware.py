import threading
from django.utils.deprecation import MiddlewareMixin

# Thread-local storage for request context
_thread_locals = threading.local()

class RequestContextMiddleware(MiddlewareMixin):
    """Middleware to store the current request in thread-local storage"""
    
    def process_request(self, request):
        _thread_locals.request = request
        return None
    
    def process_response(self, request, response):
        if hasattr(_thread_locals, 'request'):
            del _thread_locals.request
        return response

def get_current_request():
    """Get the current request from thread-local storage"""
    return getattr(_thread_locals, 'request', None)