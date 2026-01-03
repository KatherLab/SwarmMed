from django.shortcuts import redirect
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
            reverse("two_factor:login"),
            reverse("two_factor:setup"),
            reverse("users:signout"),
            reverse("users:signin"),
            "/static/",
            "/media/",
        ]

        if request.user.is_authenticated:
            # Check if the current path is exempt
            path = request.path
            is_exempt = any(path.startswith(exempt) for exempt in exempt_paths)

            # OTPMiddleware adds 'is_verified' to request.user
            if not is_exempt and not getattr(request.user, "is_verified", False):
                # If not verified and not on an exempt page, redirect to MFA login
                # If they haven't set up MFA, redirect to setup page.
                if not default_device(request.user):
                    return redirect("two_factor:setup")
                return redirect("two_factor:login")

        return self.get_response(request)
