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
    Pseudonymizes user-linked metadata in audit logs without modifying
    cryptographically signed log content.

    Important: We intentionally do NOT edit signed message bodies because that
    would invalidate tamper-evidence guarantees. Instead, we detach the user FK
    and replace user_identifier with a pseudonymous value.
    """
    from logs.models import LogEntry

    username = user.username
    anonymized_id = f"DELETED_USER_{user.id}"

    # Logs directly attributed to this user: remove direct user link and
    # pseudonymize identifier. Use bulk update to avoid invoking save().
    LogEntry.objects.filter(user=user).update(
        user=None,
        user_identifier=anonymized_id,
        ip_address=None,
        user_agent=None,
    )

    # Legacy or detached records keyed by old identifier.
    LogEntry.objects.filter(user_identifier=username).update(
        user_identifier=anonymized_id,
        ip_address=None,
        user_agent=None,
    )
