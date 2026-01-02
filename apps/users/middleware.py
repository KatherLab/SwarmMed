from django.shortcuts import redirect, render
from django.urls import reverse

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
                # If they haven't set up MFA, two_factor:login will handle it or we can 
                # redirect to setup if we want to force setup.
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
