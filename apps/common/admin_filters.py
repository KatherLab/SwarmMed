from django.contrib import admin
from project.models import Project


class BaseProjectFilter(admin.SimpleListFilter):
    title = "project"
    parameter_name = "project"

    def lookups(self, request, model_admin):
        projects = Project.objects.order_by("title").values_list("id", "title")
        return [(str(p[0]), p[1]) for p in projects]


class ProjectFilter_Generic(BaseProjectFilter):
    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(project__id=self.value())
        return queryset


class ProjectFilter_ByNetwork(BaseProjectFilter):
    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(network__project__id=self.value())
        return queryset


class ProjectFilter_ByJob(BaseProjectFilter):
    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(job__project__id=self.value())
        return queryset
