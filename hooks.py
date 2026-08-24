app_name = "db_connector"
app_title = "Db Connector"
app_publisher = "HKSR-DT"
app_description = "Database connector for other datasource"
app_email = "kit.ho@rehabsociety.org.hk"
app_license = "mit"

after_migrate = ["db_connector.identity_resolution_setup.after_migrate"]

doctype_js = {
    "CCD Master": "public/js/ccd_master_identity_resolution.js",
}

doctype_list_js = {
    "CCD Match Component Review": "public/js/ccd_match_component_review_list.js",
    "CCD Match Review Candidate": "public/js/ccd_match_review_candidate_list.js",
}

doc_events = {
    "CCD Master": {
        "on_update": "db_connector.api_identity_resolution.handle_ccd_master_update",
    },
}


server_script_utils = [
    # "db_connector.api.run_mssql_test",
    # "db_connector.api.connect_db",
    "db_connector.api_imis.run_macroFromERPNext"  # Add this here too
]

# This is what actually runs the scheduled task
scheduler_events = {
    "all": [
        "db_connector.api_imis.run_macroFromERPNext"
    ],
    "daily": [
        "db_connector.api_imis.run_macroFromERPNext",
        "db_connector.api_identity_qc.run_qc_monitor",
    ],
}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "db_connector",
# 		"logo": "/assets/db_connector/logo.png",
# 		"title": "Db Connector",
# 		"route": "/db_connector",
# 		"has_permission": "db_connector.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/db_connector/css/db_connector.css"
# app_include_js = "/assets/db_connector/js/db_connector.js"

# include js, css files in header of web template
# web_include_css = "/assets/db_connector/css/db_connector.css"
# web_include_js = "/assets/db_connector/js/db_connector.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "db_connector/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "db_connector/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "db_connector.utils.jinja_methods",
# 	"filters": "db_connector.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "db_connector.install.before_install"
# after_install = "db_connector.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "db_connector.uninstall.before_uninstall"
# after_uninstall = "db_connector.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "db_connector.utils.before_app_install"
# after_app_install = "db_connector.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "db_connector.utils.before_app_uninstall"
# after_app_uninstall = "db_connector.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "db_connector.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"db_connector.tasks.all"
# 	],
# 	"daily": [
# 		"db_connector.tasks.daily"
# 	],
# 	"hourly": [
# 		"db_connector.tasks.hourly"
# 	],
# 	"weekly": [
# 		"db_connector.tasks.weekly"
# 	],
# 	"monthly": [
# 		"db_connector.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "db_connector.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "db_connector.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "db_connector.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["db_connector.utils.before_request"]
# after_request = ["db_connector.utils.after_request"]

# Job Events
# ----------
# before_job = ["db_connector.utils.before_job"]
# after_job = ["db_connector.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"db_connector.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }
