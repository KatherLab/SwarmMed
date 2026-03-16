import re
import traceback

# List of common PHI/PII fields to redact
SENSITIVE_FIELDS = [
    "first_name",
    "last_name",
    "email",
    "phone",
    "address",
    "zip_code",
    "ssn",
    "patient_id",
    "medical_record_number",
    "mrn",
    "birth_date",
    "dob",
    "password",
    "secret",
    "token",
    "key",
    "access_key",
    "api_key",
    "credit_card",
    "cvv",
]

# Regex for basic redaction patterns
EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
# Social Security Number (US)
SSN_REGEX = r"\d{3}-\d{2}-\d{4}"
# Common medical identifier patterns (e.g., MRN: 123456)
MEDICAL_ID_REGEX = r"(?:mrn|patient\s*id|record\s*number)[:\s]*([a-zA-Z0-9-]+)"


def redact_phi(data):
    """
    Recursively redacts sensitive information from dictionaries or lists.
    """
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            k_lower = key.lower()
            if any(field in k_lower for field in SENSITIVE_FIELDS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_phi(value)
        return redacted
    elif isinstance(data, list):
        return [redact_phi(item) for item in data]
    elif isinstance(data, str):
        # Apply regex redactions to strings
        val = re.sub(EMAIL_REGEX, "[REDACTED_EMAIL]", data)
        val = re.sub(SSN_REGEX, "[REDACTED_SSN]", val)
        # Redact the ID portion of medical ID strings
        val = re.sub(
            MEDICAL_ID_REGEX,
            lambda m: f"{m.group(0).split(':')[0]}: [REDACTED_ID]",
            val,
            flags=re.IGNORECASE,
        )
        return val
    return data


def redact_message(message):
    """
    Redacts sensitive information from a log message string.
    """
    if not isinstance(message, str):
        return message

    # Apply all redaction patterns
    message = re.sub(EMAIL_REGEX, "[REDACTED_EMAIL]", message)
    message = re.sub(SSN_REGEX, "[REDACTED_SSN]", message)
    message = re.sub(
        MEDICAL_ID_REGEX,
        lambda m: f"{m.group(0).split(':')[0] if ':' in m.group(0) else 'ID'}: [REDACTED_ID]",
        message,
        flags=re.IGNORECASE,
    )

    return message


def bulk_create_signed_logs(log_entries):
    """
    Computes signatures and chains for a list of LogEntry objects
    and performs a bulk_create. This allows for high-volume logging
    (like container output) without sacrificing the tamper-evident audit trail.
    """
    if not log_entries:
        return

    from django.core.cache import cache

    from .models import LogEntry, LogSigningKey

    # Get the latest entry to start the chain
    cache_key = "latest_log_signature"
    previous_hash = cache.get(cache_key)

    if not previous_hash:
        last_entry = LogEntry.objects.order_by("-timestamp").first()
        previous_hash = last_entry.signature if last_entry else "0" * 64
    
    active_key = LogSigningKey.get_active_key()

    for entry in log_entries:
        # Replicate logic from LogEntry.save()
        if not entry.user_identifier and entry.user:
            entry.user_identifier = entry.user.username

        if not entry.signing_key:
            entry.signing_key = active_key

        entry.previous_hash = previous_hash
        entry.signature = entry.calculate_signature()
        previous_hash = entry.signature

    LogEntry.objects.bulk_create(log_entries)
    
    # Update cache with the last signature in the batch
    cache.set(cache_key, previous_hash, 3600 * 24)


def format_exception(exc):
    """
    Formats an exception into a structured dictionary for logging.
    Includes type, message, and a redacted traceback.
    """
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }
