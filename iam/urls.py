from django.contrib.auth.decorators import login_not_required
from django.urls import path
from mozilla_django_oidc.views import OIDCLogoutView

from .auth import AuthenticationRequestView, CallbackView
from .views import admin, base, people, policy, updates, work

urlpatterns = [
    path("", base.dashboard, name="dashboard"),
    path("login/", base.login_page, name="login"),
    path("login/denied/", base.login_denied, name="login_denied"),
    path("login/dev/", base.dev_login, name="dev_login"),
    path("logout/", login_not_required(OIDCLogoutView.as_view()), name="logout"),
    path(
        "oidc/authenticate/",
        login_not_required(AuthenticationRequestView.as_view()),
        name="oidc_authentication_init",
    ),
    path(
        "oidc/callback/",
        login_not_required(CallbackView.as_view()),
        name="oidc_authentication_callback",
    ),
    path("-/healthz", base.healthz, name="healthz"),
    path("-/readyz", base.readyz, name="readyz"),
    # People
    path("people/", people.person_list, name="person_list"),
    path("people/new/", people.person_create, name="person_create"),
    path("people/<int:pk>/", people.person_detail, name="person_detail"),
    path("people/<int:pk>/edit/", people.person_edit, name="person_edit"),
    path("people/<int:pk>/start/<str:kind>/", people.person_start, name="person_start"),
    path("people/<int:pk>/archive/", people.person_archive, name="person_archive"),
    # Policy
    path("teams/", policy.team_list, name="team_list"),
    path("teams/new/", policy.team_form, name="team_create"),
    path("teams/<int:pk>/", policy.team_detail, name="team_detail"),
    path("teams/<int:pk>/edit/", policy.team_form, name="team_edit"),
    path("teams/<int:pk>/delete/", policy.team_delete, name="team_delete"),
    path("profiles/", policy.profile_list, name="profile_list"),
    path("profiles/new/", policy.profile_form, name="profile_create"),
    path("profiles/<int:pk>/", policy.profile_detail, name="profile_detail"),
    path("profiles/<int:pk>/edit/", policy.profile_form, name="profile_edit"),
    path("profiles/<int:pk>/delete/", policy.profile_delete, name="profile_delete"),
    # Work
    path("workflows/", work.workflow_list, name="workflow_list"),
    path("workflows/<int:pk>/", work.workflow_detail, name="workflow_detail"),
    path("workflows/<int:pk>/cancel/", work.workflow_cancel, name="workflow_cancel"),
    path("jobs/", work.job_list, name="job_list"),
    path("jobs/<int:pk>/", work.job_detail, name="job_detail"),
    path("jobs/<int:pk>/retry/", work.job_retry, name="job_retry"),
    path("jobs/<int:pk>/approve/", work.job_approve, name="job_approve"),
    path("jobs/<int:pk>/reject/", work.job_reject, name="job_reject"),
    path("tasks/", work.task_list, name="task_list"),
    path("tasks/<int:pk>/", work.task_detail, name="task_detail"),
    path("tasks/<int:pk>/complete/", work.task_complete, name="task_complete"),
    path("tasks/<int:pk>/cancel/", work.task_cancel, name="task_cancel"),
    path("audit/", work.audit_list, name="audit_list"),
    # Admin
    path("admin/connections/", admin.connection_list, name="connection_list"),
    path("admin/connections/new/", admin.connection_choose, name="connection_choose"),
    path("admin/connections/new/<str:adapter>/", admin.connection_create, name="connection_create"),
    path("admin/connections/<int:pk>/", admin.connection_detail, name="connection_detail"),
    path("admin/connections/<int:pk>/edit/", admin.connection_edit, name="connection_edit"),
    path("admin/connections/<int:pk>/test/", admin.connection_test, name="connection_test"),
    path("admin/connections/<int:pk>/toggle/", admin.connection_toggle, name="connection_toggle"),
    path(
        "admin/connections/<int:pk>/import-groups/",
        admin.connection_import_groups,
        name="connection_import_groups",
    ),
    path("admin/connections/<int:pk>/sync/", admin.connection_sync, name="connection_sync"),
    path(
        "admin/connections/<int:pk>/groups/",
        admin.connection_add_group,
        name="connection_add_group",
    ),
    path(
        "admin/connections/<int:pk>/groups/<int:group_pk>/remove/",
        admin.connection_remove_group,
        name="connection_remove_group",
    ),
    path("admin/connections/<int:pk>/delete/", admin.connection_delete, name="connection_delete"),
    path("admin/users/", admin.user_list, name="user_list"),
    path("admin/users/new/", admin.user_form, name="user_create"),
    path("admin/users/<int:pk>/", admin.user_form, name="user_edit"),
    path("admin/settings/", admin.settings_form, name="settings"),
    # In-app updates: JSON for the update button and the settings section
    path("admin/updates/", updates.status, name="updates"),
    path("admin/updates/check/", updates.check, name="updates_check"),
    path("admin/updates/install/", updates.install, name="updates_install"),
    path("admin/updates/channel/", updates.channel, name="updates_channel"),
]
