"""Custom database fields for the common application.

Provides encrypted field classes using Fernet encryption for storing
sensitive data securely in the database.
"""

import base64
import threading

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
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
    """A mixin to handle Fernet encryption/decryption for Django model fields.

    Provides compatibility with django-fernet-fields by using the same
    HKDF key derivation when FERNET_USE_HKDF is enabled.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the encrypted field mixin.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.
        """
        super().__init__(*args, **kwargs)

    @property
    def fernet(self):
        """Retrieves a cached MultiFernet instance or creates a new one.

        Thread-safe caching ensures HKDF is only run once per unique set of keys.

        Returns:
            MultiFernet: The Fernet instance for encryption/decryption.
        """
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
        """Creates a MultiFernet instance from the provided keys.

        Args:
            keys (list): A list of encryption keys.

        Returns:
            MultiFernet: The initialized MultiFernet instance.
        """
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
                derived_key = base64.urlsafe_b64encode(
                    hkdf.derive(force_bytes(key))
                )
                fernet_keys.append(Fernet(derived_key))
            else:
                # If not using HKDF, the key must already be a valid Fernet key
                # (32 url-safe base64-encoded bytes).
                fernet_keys.append(Fernet(key))

        return MultiFernet(fernet_keys)

    def get_prep_value(self, value):
        """Encrypts the value before saving to the database.

        Args:
            value (Any): The raw value to be encrypted.

        Returns:
            str: The encrypted value.
        """
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        # Encrypt the value and return it as a string for the database
        return self.fernet.encrypt(force_bytes(value)).decode("utf-8")

    def from_db_value(self, value, expression, connection):
        """Decrypts the value after retrieving from the database.

        Args:
            value (Any): The encrypted value from the database.
            expression (Any): The expression.
            connection (Any): The database connection.

        Returns:
            str: The decrypted value.
        """
        if value is None or value == "":
            return value
        try:
            return force_str(self.fernet.decrypt(force_bytes(value)))
        except (InvalidToken, TypeError, ValueError):
            # In case of decryption failure, return the raw value
            # This can happen during migrations or if the key changed
            return value

    def to_python(self, value):
        """Ensures the value is decrypted when converted to a Python object.

        Args:
            value (Any): The value to convert.

        Returns:
            str: The decrypted value if it was encrypted, otherwise the raw value.
        """
        if value is None or value == "":
            return value

        # If the value is already decrypted (e.g., from form data),
        # attempting to decrypt it will fail.
        try:
            # We try to decrypt only if it looks like a Fernet token (usually starts with gAAAA)
            if isinstance(value, (str, bytes)) and force_bytes(
                value
            ).startswith(b"gAAAA"):
                return force_str(self.fernet.decrypt(force_bytes(value)))
        except (InvalidToken, TypeError, ValueError):
            return value

        return super().to_python(value)

    def deconstruct(self):
        """Returns a 4-tuple for reconstructing the field during migrations.

        Returns:
            tuple: (name, path, args, kwargs)
        """
        name, path, args, kwargs = super().deconstruct()
        # Ensure migrations use the base Django field classes
        return name, path, args, kwargs


class EncryptedCharField(EncryptedFieldMixin, models.CharField):
    """A CharField that stores encrypted data.

    Note: max_length applies to the UNENCRYPTED value.
    The database column should be large enough to hold the encrypted version.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the encrypted CharField.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.
        """
        # We don't want to enforce max_length on the encrypted string at the DB level
        # through Django's validation, but CharField requires it.
        # django-fernet-fields usually uses a TextField under the hood or a large CharField.
        super().__init__(*args, **kwargs)

    def get_internal_type(self):
        """Returns the internal type of the field.

        Returns:
            str: "TextField"
        """
        return "TextField"


class EncryptedTextField(EncryptedFieldMixin, models.TextField):
    """A TextField that stores encrypted data."""

    def get_internal_type(self):
        """Returns the internal type of the field.

        Returns:
            str: "TextField"
        """
        return "TextField"
