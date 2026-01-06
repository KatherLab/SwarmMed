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
    pass


class SetupView(BaseSetupView):
    success_url = "users:two_factor:setup_complete"
    qrcode_url = "users:two_factor:qr"


class QRGeneratorView(BaseQRGeneratorView):
    pass


class SetupCompleteView(BaseSetupCompleteView):
    pass


class BackupTokensView(BaseBackupTokensView):
    success_url = "users:two_factor:backup_tokens"


class ProfileView(BaseProfileView):
    pass


class DisableView(BaseDisableView):
    success_url = reverse_lazy("users:two_factor:profile")

    def dispatch(self, *args, **kwargs):
        # Redirect to login if not verified, to force re-verification
        # instead of silently redirecting to success_url (profile)
        fn = otp_required(
            super().dispatch,
            login_url="users:two_factor:login",
            redirect_field_name="next",
        )
        return fn(*args, **kwargs)

    def form_valid(self, form):
        messages.success(
            self.request, "Two-factor authentication has been disabled."
        )
        return super().form_valid(form)
