from django.contrib import admin
from django.urls import include, path, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve 

urlpatterns = [
    path("", include("home.urls")),
    path("admin/", admin.site.urls),
    path("users/", include("apps.users.urls")),
    path("project/", include("apps.project.urls")),
    path("data/", include("apps.data.urls")),
    path("network/", include("apps.network.urls")),
    path("training/", include("apps.training.urls")),
    path("results/", include("apps.results.urls")),
    path("logs/", include("apps.logs.urls")),
    path("communication/", include("apps.communication.urls")),
    
    path("__debug__/", include("debug_toolbar.urls")),

    re_path(r'^media/(?P<path>.*)$', serve,{'document_root': settings.MEDIA_ROOT}), 
    re_path(r'^static/(?P<path>.*)$', serve,{'document_root': settings.STATIC_ROOT}),     
]

urlpatterns += static(settings.MEDIA_URL      , document_root=settings.MEDIA_ROOT     )
