"""
Common views for the application, including legal and static pages.
"""

from django.shortcuts import render


def privacy_policy(request):
    """Displays the privacy policy page."""
    return render(request, "apps/common/privacy.html", {"segment": "privacy"})


def terms_and_conditions(request):
    """Displays the terms and conditions page."""
    return render(request, "apps/common/terms.html", {"segment": "terms"})


def license(request):
    """Displays the license page."""
    return render(request, "apps/common/license.html", {"segment": "license"})


def imprint(request):
    """Displays the imprint page."""
    return render(request, "apps/common/imprint.html", {"segment": "imprint"})


def contact(request):
    """Displays the contact page."""
    return render(request, "apps/common/contact.html", {"segment": "contact"})
