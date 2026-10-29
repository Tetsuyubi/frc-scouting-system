from django.urls import path
from . import views

app_name = 'hours'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('onboarding/', views.onboarding, name='onboarding'),
    path('profile/', views.profile, name='profile'),
    path('check-in-out/', views.check_in_out, name='check_in_out'),
    path('rankings/', views.rankings, name='rankings'),
    path('checked-in/', views.checked_in_users, name='checked_in'),
    path('user/<int:user_id>/', views.user_detail, name='user_detail'),
    path('user/<int:user_id>/adjust/', views.adjust_hours, name='adjust_hours'),
    path('adjustment/<int:adjustment_id>/delete/', views.delete_adjustment, name='delete_adjustment'),
    path('badge/toggle/', views.toggle_badge, name='toggle_badge'),
    path('badge/acknowledge/', views.acknowledge_badges, name='acknowledge_badges'),
    path('avatar/reset/', views.reset_avatar, name='reset_avatar'),
    path('scout-leaderboard/', views.scout_leaderboard, name='scout_leaderboard'),
]
