"""Context processors for the common app.

These functions are used to inject common variables into the template context
for all rendered pages, such as organization and contact information.
"""

from django.conf import settings


def legal_and_contact_info(request):
    """Exposes legal and contact information from settings to all templates.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        dict: A dictionary of common variables for templates.
    """
    return {
        "organization_name": settings.ORGANIZATION_NAME,
        "organization_street": settings.ORGANIZATION_STREET,
        "organization_zip_city": settings.ORGANIZATION_ZIP_CITY,
        "organization_country": settings.ORGANIZATION_COUNTRY,
        "organization_website": settings.ORGANIZATION_WEBSITE,
        "representative_name": settings.REPRESENTATIVE_NAME,
        "contact_email": settings.CONTACT_EMAIL,
        "contact_phone": settings.CONTACT_PHONE,
        "editorial_responsible_name": settings.EDITORIAL_RESPONSIBLE_NAME,
        "editorial_responsible_address": settings.EDITORIAL_RESPONSIBLE_ADDRESS,
        # Privacy policy specific variables
        "privacy_controller_name": settings.PRIVACY_CONTROLLER_NAME,
        "privacy_controller_address": settings.PRIVACY_CONTROLLER_ADDRESS,
        "privacy_contact_email": settings.PRIVACY_CONTACT_EMAIL,
        "privacy_dpo_email": settings.PRIVACY_DPO_EMAIL,
        "privacy_dpo_address": settings.PRIVACY_DPO_ADDRESS,
        "privacy_hosting_provider": settings.PRIVACY_HOSTING_PROVIDER,
        "privacy_data_region": settings.PRIVACY_DATA_REGION,
        "account_erasure_grace_days": settings.ACCOUNT_ERASURE_GRACE_DAYS,
        "security_log_retention_days": settings.SECURITY_LOG_RETENTION_DAYS,
        "ip_anonymization_days": settings.IP_ANONYMIZATION_DAYS,
    }
