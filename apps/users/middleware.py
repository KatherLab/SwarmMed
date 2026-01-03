from django.shortcuts import redirect, render
from django.urls import reverse
from two_factor.utils import default_device

class MFAEnforcementMiddleware:
    """
    Middleware that enforces MFA for all authenticated users.
    If a user is logged in but not MFA-verified, they are redirected
    to the MFA login or setup page.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Exempt paths that must be accessible without MFA
        exempt_paths = [
            reverse('two_factor:login'),
            reverse('two_factor:setup'),
            reverse('users:signout'),
            reverse('users:signin'),
            '/static/',
            '/media/',
        ]

        if request.user.is_authenticated:
            # Check if the current path is exempt
            path = request.path
            is_exempt = any(path.startswith(exempt) for exempt in exempt_paths)

            # OTPMiddleware adds 'is_verified' to request.user
            if not is_exempt and not getattr(request.user, 'is_verified', False):
                # If not verified and not on an exempt page, redirect to MFA login
                # If they haven't set up MFA, redirect to setup page.
                if not default_device(request.user):
                    return redirect('two_factor:setup')
                return redirect('two_factor:login')

        return self.get_response(request)


class GDPRRestrictionMiddleware:
    """
    Middleware that enforces the GDPR Right to Restriction.
    If a user's profile is marked as 'is_restricted', they are blocked 
    from accessing most platform features until the restriction is lifted.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Exempt paths that remain accessible during restriction (e.g., policy, support)
        exempt_paths = [
            reverse('users:privacy_policy'),
            reverse('users:signout'),
            '/static/',
        ]

        if request.user.is_authenticated:
            profile = getattr(request.user, 'profile', None)
            if profile and profile.is_restricted:
                path = request.path
                is_exempt = any(path.startswith(exempt) for exempt in exempt_paths)
                
                if not is_exempt:
                    # Return a dedicated 'Account Restricted' page or 403
                    return render(request, 'errors/restricted.html', status=403)

        return self.get_response(request)


class LegalAcceptanceMiddleware:
    """
    Middleware that ensures authenticated users have accepted the 
    Terms and Conditions and Privacy Policy.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            # List of URLs that don't require legal acceptance check
            # to avoid redirect loops and allow the user to actually accept or sign out.
            exempt_urls = [
                reverse('users:accept_terms'),
                reverse('users:signout'),
                reverse('terms'),
                reverse('privacy'),
                reverse('license'),
                reverse('imprint'),
                reverse('contact'),
            ]
            
            # Also exempt static and media files
            if request.path.startswith('/static/') or request.path.startswith('/media/'):
                return self.get_response(request)

            if request.path not in exempt_urls:
                profile = getattr(request.user, 'profile', None)
                if profile and (not profile.accepted_terms or not profile.accepted_policy):
                    return redirect('users:accept_terms')

        return self.get_response(request)
