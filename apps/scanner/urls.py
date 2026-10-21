from django.urls import path

from . import views

app_name = "scanner"
urlpatterns = [
    path("", views.index, name="index"),
    path("sw.js", views.service_worker, name="sw"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
]
