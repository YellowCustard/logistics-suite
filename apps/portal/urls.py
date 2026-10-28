from django.urls import path, re_path

from . import views

app_name = "portal"
# Every path under /portal/ serves the app; React Router picks the screen.
urlpatterns = [path("", views.index, name="index"), re_path(r"^.+$", views.index)]
