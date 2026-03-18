from django.shortcuts import redirect, render
from django.urls import reverse


class GDPRRestrictionMiddleware:
    """Middleware that enforces the GDPR Right to Restriction.

    If a user's profile is marked as 'is_restricted', they are blocked
    from accessing most platform features until the restriction is lifted.

    Attributes:
        get_response (Callable): The next middleware or view in the chain.
    """

    def __init__(self, get_response):
        """Initialize the middleware.

        Args:
            get_response (Callable): The next middleware or view in the chain.
        """
        self.get_response = get_response

    def __call__(self, request):
        """Handle the incoming request.

        Args:
            request (HttpRequest): The incoming HTTP request.

        Returns:
            HttpResponse: The response from the next middleware or view, or a 403 error.
        """
        # Exempt paths that remain accessible during restriction (e.g., policy, support)
        exempt_paths = [
            reverse("privacy"),
            reverse("users:signout"),
            "/static/",
        ]

        if request.user.is_authenticated:
            profile = getattr(request.user, "profile", None)
            if profile and profile.is_restricted:
                path = request.path
                is_exempt = any(
                    path.startswith(exempt) for exempt in exempt_paths
                )

                if not is_exempt:
                    # Return a dedicated 'Account Restricted' page or 403
                    return render(
                        request, "errors/restricted.html", status=403
                    )

        return self.get_response(request)


class LegalAcceptanceMiddleware:
    """Middleware that ensures authenticated users have accepted the Terms and Conditions and Privacy Policy.

    Attributes:
        get_response (Callable): The next middleware or view in the chain.
    """

    def __init__(self, get_response):
        """Initialize the middleware.

        Args:
            get_response (Callable): The next middleware or view in the chain.
        """
        self.get_response = get_response

    def __call__(self, request):
        """Handle the incoming request.

        Args:
            request (HttpRequest): The incoming HTTP request.

        Returns:
            HttpResponse: The response from the next middleware or view, or a redirect.
        """
        if request.user.is_authenticated:
            # List of URLs that don't require legal acceptance check
            # to avoid redirect loops and allow the user to actually accept or sign out.
            exempt_urls = [
                reverse("users:accept_terms"),
                reverse("users:signout"),
                reverse("terms"),
                reverse("privacy"),
                reverse("license"),
                reverse("imprint"),
                reverse("contact"),
            ]

            # Also exempt static and media files
            if request.path.startswith("/static/") or request.path.startswith(
                "/media/"
            ):
                return self.get_response(request)

            if request.path not in exempt_urls:
                profile = getattr(request.user, "profile", None)
                if profile and (
                    not profile.accepted_terms or not profile.accepted_policy
                ):
                    return redirect("users:accept_terms")

        return self.get_response(request)
