"""
Utility functions for the users application.
Includes helper functions for filtering user lists based on request parameters.
"""


def user_filter(request):
    """
    Parses the GET parameters from an HTTP request and converts them into
    a dictionary of Django database filters.

    Currently supports:
    - search: Filters by username (case-insensitive contains).
    """
    filter_dict = {}

    # Mapping of frontend parameter names to backend Django filter keys.
    filter_mappings = {"search": "username__icontains"}

    # Iterate through all parameters in the URL (e.g., ?search=alice&page=2).
    for key in request.GET:
        # We only care about keys that are in our mapping and have a non-empty value.
        # We skip 'page' as it's used for pagination, not database filtering.
        if key in filter_mappings and request.GET.get(key) and key != "page":
            db_filter_key = filter_mappings[key]
            filter_dict[db_filter_key] = request.GET.get(key)

    return filter_dict


def anonymize_user_data(user):
    """
    Anonymizes all personal data associated with a user in the audit logs.
    This replaces the username and potentially sensitive info in log messages
    while preserving the audit trail for system security analysis.
    This is required for GDPR 'Right to Erasure' compliance.
    """
    from logs.models import LogEntry

    username = user.username
    email = user.email
    full_name = (
        getattr(user.profile, "full_name", "")
        if hasattr(user, "profile")
        else ""
    )

    # Replacement string
    anonymized_id = f"DELETED_USER_{user.id}"

    # Find all LogEntry records associated with this user via identifier
    # (includes logs where user object might already be NULL but identifier remains)
    logs = LogEntry.objects.filter(user_identifier=username)

    for entry in logs:
        # Replace username in identifier
        entry.user_identifier = anonymized_id

        # Scrub message
        if username and username in entry.message:
            entry.message = entry.message.replace(username, anonymized_id)
        if email and email in entry.message:
            entry.message = entry.message.replace(email, "[EMAIL_REDACTED]")
        if full_name and full_name in entry.message:
            entry.message = entry.message.replace(full_name, "[NAME_REDACTED]")

        # Scrub context_data
        if entry.context_data:
            new_context = entry.context_data.copy()
            for key, value in new_context.items():
                if isinstance(value, str):
                    if username and username in value:
                        new_context[key] = value.replace(
                            username, anonymized_id
                        )
                    if email and email in value:
                        new_context[key] = value.replace(
                            email, "[EMAIL_REDACTED]"
                        )
            entry.context_data = new_context

        # Save the modified entry.
        # Note: This will invalidate the cryptographic signature of the log entry,
        # which is an expected side-effect of post-hoc anonymization.
        entry.save()
