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
    "birth_date",
    "password",
    "secret",
    "token",
    "key",
]

# Regex for basic email redaction
EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"


def redact_phi(data):
    """
    Recursively redacts sensitive information from dictionaries or lists.
    """
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            if any(field in key.lower() for field in SENSITIVE_FIELDS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_phi(value)
        return redacted
    elif isinstance(data, list):
        return [redact_phi(item) for item in data]
    elif isinstance(data, str):
        # Redact emails in strings
        return re.sub(EMAIL_REGEX, "[REDACTED_EMAIL]", data)
    return data


def redact_message(message):
    """
    Redacts sensitive information from a log message string.
    """
    if not isinstance(message, str):
        return message

    # Redact emails
    message = re.sub(EMAIL_REGEX, "[REDACTED_EMAIL]", message)

    # You could add more regex-based redactions here (e.g., SSN, phone numbers)
    return message


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
