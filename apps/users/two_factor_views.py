"""Views for two-factor authentication in the users application.

Extends standard two-factor authentication views with project-specific 
configuration and custom overrides.
"""

from django.contrib import messages
from django.urls import reverse_lazy
from django_otp.decorators import otp_required
from two_factor.views import (
    BackupTokensView as BaseBackupTokensView,
)
from two_factor.views import (
    DisableView as BaseDisableView,
)
from two_factor.views import (
    LoginView as BaseLoginView,
)
from two_factor.views import (
    ProfileView as BaseProfileView,
)
from two_factor.views import (
    QRGeneratorView as BaseQRGeneratorView,
)
from two_factor.views import (
    SetupCompleteView as BaseSetupCompleteView,
)
from two_factor.views import (
    SetupView as BaseSetupView,
)


class LoginView(BaseLoginView):
    """View for user login with two-factor authentication support."""
    pass


class SetupView(BaseSetupView):
    """View for setting up two-factor authentication for a user account.

    Attributes:
        success_url (str): URL to redirect to after successful setup.
        qrcode_url (str): URL for the QR code generator.
    """
    success_url = "users:two_factor:setup_complete"
    qrcode_url = "users:two_factor:qr"


class QRGeneratorView(BaseQRGeneratorView):
    """View that generates a QR code for two-factor authentication setup."""
    pass


class SetupCompleteView(BaseSetupCompleteView):
    """View displayed to the user after two-factor authentication is enabled."""
    pass


class BackupTokensView(BaseBackupTokensView):
    """View for generating and viewing backup tokens for two-factor authentication.

    Attributes:
        success_url (str): URL to redirect to after successful token generation.
    """
    success_url = "users:two_factor:backup_tokens"


class ProfileView(BaseProfileView):
    """View for managing two-factor authentication settings in the user profile."""
    pass


class DisableView(BaseDisableView):
    """View for disabling two-factor authentication for a user account.

    Attributes:
        success_url (str): URL to redirect to after 2FA is disabled.
    """
    success_url = reverse_lazy("users:two_factor:profile")

    def dispatch(self, *args, **kwargs):
        """Dispatches the request, requiring OTP verification.

        Redirects to login if not verified, to force re-verification
        instead of silently redirecting to success_url (profile).

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            HttpResponse: The rendered response.
        """
        fn = otp_required(
            super().dispatch,
            login_url="users:two_factor:login",
            redirect_field_name="next",
        )
        return fn(*args, **kwargs)

    def form_valid(self, form):
        """Handles a valid form submission for disabling 2FA.

        Args:
            form (Form): The valid form instance.

        Returns:
            HttpResponse: A redirect to the success URL.
        """
        messages.success(
            self.request, "Two-factor authentication has been disabled."
        )
        return super().form_valid(form)
