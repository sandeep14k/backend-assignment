from django.urls import path

from planner import views

urlpatterns = [
    path("", views.map_view, name="home"),
    path("map/", views.map_view, name="map"),
    path("api/route/", views.route, name="route"),
    path("health/", views.health, name="health"),
]
