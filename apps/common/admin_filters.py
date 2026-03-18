"""Admin filters for the common application.

Provides reusable filter classes for the Django admin interface,
specifically for filtering objects by project.
"""

from django.contrib import admin

from project.models import Project


class BaseProjectFilter(admin.SimpleListFilter):
    """Base class for project-based filters in the admin interface.

    Attributes:
        title (str): The display title for the filter in the admin sidebar.
        parameter_name (str): The URL query parameter used for this filter.
    """

    title = "project"
    parameter_name = "project"

    def lookups(self, request, model_admin):
        """Returns a list of tuples representing the projects available for filtering.

        Args:
            request (HttpRequest): The current HTTP request.
            model_admin (ModelAdmin): The ModelAdmin instance.

        Returns:
            list: A list of (id, title) tuples for all projects.
        """
        projects = Project.objects.order_by("title").values_list("id", "title")
        return [(str(p[0]), p[1]) for p in projects]


class ProjectFilter_Generic(BaseProjectFilter):
    """A generic project filter that applies directly to a 'project' field."""

    def queryset(self, request, queryset):
        """Filters the queryset by the selected project ID.

        Args:
            request (HttpRequest): The current HTTP request.
            queryset (QuerySet): The queryset to filter.

        Returns:
            QuerySet: The filtered queryset.
        """
        if self.value():
            return queryset.filter(project__id=self.value())
        return queryset


class ProjectFilter_ByNetwork(BaseProjectFilter):
    """Filters objects by project through a related 'network' field."""

    def queryset(self, request, queryset):
        """Filters the queryset by the project associated with its network.

        Args:
            request (HttpRequest): The current HTTP request.
            queryset (QuerySet): The queryset to filter.

        Returns:
            QuerySet: The filtered queryset.
        """
        if self.value():
            return queryset.filter(network__project__id=self.value())
        return queryset


class ProjectFilter_ByJob(BaseProjectFilter):
    """Filters objects by project through a related 'job' field."""

    def queryset(self, request, queryset):
        """Filters the queryset by the project associated with its job.

        Args:
            request (HttpRequest): The current HTTP request.
            queryset (QuerySet): The queryset to filter.

        Returns:
            QuerySet: The filtered queryset.
        """
        if self.value():
            return queryset.filter(job__project__id=self.value())
        return queryset
