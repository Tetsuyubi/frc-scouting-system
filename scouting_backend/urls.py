from django.conf.urls.static import static
from django.conf import settings
from django.contrib import admin
from django.urls import path, include

from scouting_backend import settings
from teams import views as team_views
from scanner import views as scanner_views
from strategy import views as strategy_views
from strategy import alert_views
from strategy import simulation_views
from authenticate import views as auth_views
from authenticate import views_admin as admin_views
from plugins import plugin_manager

# Add message framework support
from django.contrib.messages import get_messages

urlpatterns = [
    path('admin/', admin.site.urls),
    # ... your other paths ...
]

# This allows Django to serve static files during development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")

urlpatterns = [
    # path('admin/', admin.site.urls),  # Disabled Django admin
    path('', team_views.home, name='home'),
    path('scanner/', scanner_views.scanner, name='scanner'),
    path('scanner/sw.js', scanner_views.service_worker, name='scanner_sw'),
    path('teams/', team_views.display_teams, name='teams'),
    path('teams/compare/', team_views.compare_teams, name='compare_teams'),
    path('teams/<int:team_number>/', team_views.team_page, name='team_page'),
    path('teams/<int:team_number>/clear-pit/', team_views.clear_pit_data, name='clear_pit_data'),
    path('teams/<int:team_number>/copy-pit/', team_views.copy_pit_data, name='copy_pit_data'),
    path('teams/copy-pit-bulk/', team_views.bulk_copy_pit_data, name='bulk_copy_pit_data'),
    path('teams/export.csv', team_views.export_matches_csv, name='export_matches_csv'),
    path('teams/scout-accuracy/', team_views.scout_accuracy, name='scout_accuracy'),
    path('teams/<int:team_number>/notes/', team_views.save_team_notes, name='save_team_notes'),
    path("teams/human-scout/<int:team_number>/", team_views.human_player_submit, name="human-scout"),
    path('get_events/', team_views.get_events),
    path('api/team_matches_list/', team_views.team_list_api, name='team_list_api'),
    path('api/team_avatars/', team_views.team_avatars_api, name='team_avatars_api'),
    path('pit_scouting/<int:team_number>/', team_views.pit_scouting, name='pit_scouting'),
    path('strategy/rankings/', strategy_views.rankings, name='rankings'),
    path('strategy/dashboard/', strategy_views.dashboard, name='dashboard'),
    path('strategy/picklist/', strategy_views.picklist, name='picklist'),
    path('strategy/picklist/submit/', strategy_views.picklist_submit, name='picklist_submit'),
    path('strategy/picklist/flags/', strategy_views.picklist_flags, name='picklist_flags'),
    path('api/get_path_data/<int:team_number>/', strategy_views.get_path_data, name='get_path_data'),
    path('api/team_matches/<int:team_number>/', strategy_views.team_matches_detail, name='team_matches_detail'),
    path('api/match_schedule/', strategy_views.match_schedule_api, name='match_schedule_api'),
    path('api/team_info/<int:team_number>/', strategy_views.team_info, name='team_info'),
    path('api/web_stats/', strategy_views.web_stats, name='web_stats'),
    path('api/toggle_exclude_match/', strategy_views.toggle_exclude_match, name='toggle_exclude_match'),
    path('api/tba_videos/', team_views.tba_videos, name='tba_videos'),
    path('api/alerts/', alert_views.get_broken_alerts, name='broken_alerts'),
    path('api/simulate/', simulation_views.simulate_match_view, name='simulate_match'),
    path("auth/", include("authenticate.urls")),
    path('admin-panel/', admin_views.admin_panel, name='admin_panel'),
    path('admin-panel/add-user/', admin_views.add_user, name='add_user'),
    path('admin-panel/remove-user/<int:user_id>/', admin_views.remove_user, name='remove_user'),
    path('admin-panel/update-role/<int:user_id>/', admin_views.update_user_role, name='update_user_role'),
    path('admin-panel/match-editor/', admin_views.match_data_editor, name='match_editor'),
    path('admin-panel/api/get-teams/', admin_views.get_teams_for_event, name='get_teams'),
    path('admin-panel/api/get-quantifiers/', admin_views.get_quantifiers_for_team, name='get_quantifiers'),
    path('admin-panel/api/get-matches/', admin_views.get_matches_for_team, name='get_matches'),
    path('admin-panel/api/load-match/', admin_views.load_match_data, name='load_match'),
    path('admin-panel/update-match/', admin_views.update_match_data, name='update_match'),
    path('set-theme/', auth_views.set_theme, name='set_theme'),
    # Plugin management
    path('admin-panel/plugins/enable/', admin_views.plugins_enable, name='plugins_enable'),
    path('admin-panel/plugins/upload/', admin_views.plugins_upload, name='plugins_upload'),
    path('admin-panel/plugins/install-deps/', admin_views.plugins_install_deps, name='plugins_install_deps'),
    path('admin-panel/theme/', admin_views.admin_set_theme_color, name='admin_set_theme_color'),
    path('admin-panel/logo/', admin_views.admin_set_logo_url, name='admin_set_logo_url'),
    path('admin-panel/hours/', admin_views.hours_overview, name='hours_overview'),
    path('admin-panel/hours/checkout-all/', admin_views.hours_checkout_all, name='hours_checkout_all'),
    path('admin-panel/hours/season/end/', admin_views.hours_end_season, name='hours_end_season'),
    path('admin-panel/hours/season/start/', admin_views.hours_start_season, name='hours_start_season'),
    path('admin-panel/members/', admin_views.member_profiles, name='member_profiles'),
    path('admin-panel/members/<int:user_id>/reset-avatar/', admin_views.member_reset_avatar, name='member_reset_avatar'),
    path('hours/', include('hours_app.urls')),
]

# Add plugin URLs
urlpatterns += plugin_manager.get_plugin_urls()
