from django.urls import path
from planner import views
urlpatterns = [path("", views.index), path("api/route/", views.route), path("health/", views.health)]
