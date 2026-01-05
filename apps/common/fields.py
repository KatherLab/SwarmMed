import base64
import threading
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.db import models
from django.utils.encoding import force_bytes, force_str

# Module-level cache for Fernet instances to avoid expensive HKDF derivation
# on every field access or model instantiation.
_fernet_cache = {}
_fernet_lock = threading.Lock()


class EncryptedFieldMixin:
    """
    A mixin to handle Fernet encryption/decryption for Django model fields.
    Provides compatibility with django-fernet-fields by using the same
    HKDF key derivation when FERNET_USE_HKDF is enabled.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def fernet(self):
        """
        Retrieves a cached MultiFernet instance or creates a new one.
        Thread-safe caching ensures HKDF is only run once per unique set of keys.
        """
        global _fernet_cache

        # We use the keys themselves (or their hash) as the cache key
        keys = getattr(settings, "FERNET_KEYS", [settings.SECRET_KEY])
        if isinstance(keys, (str, bytes)):
            keys = [keys]

        # Convert keys to a tuple for hashability
        cache_key = tuple(keys)

        if cache_key not in _fernet_cache:
            with _fernet_lock:
                # Double-check pattern
                if cache_key not in _fernet_cache:
                    _fernet_cache[cache_key] = self._get_fernet(keys)

        return _fernet_cache[cache_key]

    def _get_fernet(self, keys):
        use_hkdf = getattr(settings, "FERNET_USE_HKDF", True)
        fernet_keys = []

        for key in keys:
            if use_hkdf:
                hkdf = HKDF(
                    algorithm=hashes.SHA256(),
                    length=32,
                    salt=None,
                    info=b"django-fernet-fields",
                )
                derived_key = base64.urlsafe_b64encode(hkdf.derive(force_bytes(key)))
                fernet_keys.append(Fernet(derived_key))
            else:
                # If not using HKDF, the key must already be a valid Fernet key
                # (32 url-safe base64-encoded bytes).
                fernet_keys.append(Fernet(key))

        return MultiFernet(fernet_keys)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        # Encrypt the value and return it as a string for the database
        return self.fernet.encrypt(force_bytes(value)).decode("utf-8")

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return force_str(self.fernet.decrypt(force_bytes(value)))
        except Exception:
            # In case of decryption failure, return the raw value
            # This can happen during migrations or if the key changed
            return value

    def to_python(self, value):
        if value is None or value == "":
            return value

        # If the value is already decrypted (e.g., from form data),
        # attempting to decrypt it will fail.
        try:
            # We try to decrypt only if it looks like a Fernet token (usually starts with gAAAA)
            if isinstance(value, (str, bytes)) and force_bytes(value).startswith(
                b"gAAAA"
            ):
                return force_str(self.fernet.decrypt(force_bytes(value)))
        except Exception:
            pass

        return super().to_python(value)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        # Ensure migrations use the base Django field classes
        return name, path, args, kwargs


class EncryptedCharField(EncryptedFieldMixin, models.CharField):
    """
    A CharField that stores encrypted data.
    Note: max_length applies to the UNENCRYPTED value.
    The database column should be large enough to hold the encrypted version.
    """

    def __init__(self, *args, **kwargs):
        # We don't want to enforce max_length on the encrypted string at the DB level
        # through Django's validation, but CharField requires it.
        # django-fernet-fields usually uses a TextField under the hood or a large CharField.
        super().__init__(*args, **kwargs)

    def get_internal_type(self):
        return "TextField"


class EncryptedTextField(EncryptedFieldMixin, models.TextField):
    """
    A TextField that stores encrypted data.
    """

    def get_internal_type(self):
        return "TextField"
