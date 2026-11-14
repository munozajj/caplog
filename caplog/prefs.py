"""Names and defaults for settings stored in the database.

These are not secrets. Passwords, session tokens, and certificate keys
are never stored under these keys.
"""

DEFAULTS: dict[str, str] = {
    "post_mode": "both",
    "users_may_change_display": "true",
    "site_fqdn": "",
    "https_enabled": "false",
    "https_port": "8443",
    "http_redirect": "false",
    "cert_warn_days": "30",
    "ntp_enabled": "false",
    "ntp_server": "",
    "ntp_threshold": "2",
    "max_entry_length": "500",
    "accent_color": "#0d6e6a",
    "show_ips": "true",
    "guest_name_rules": "true",
    "feature_search": "true",
    "feature_tags": "true",
    "feature_notice": "true",
    "feature_corrections": "true",
    "feature_rate_limit": "false",
    "feature_scheduled_export": "false",
    "feature_display_page": "true",
    "show_number": "true",
    "show_name": "true",
    "show_time": "true",
    "show_ip": "true",
    "show_poster": "true",
    "show_tags": "true",
    "time_zone": "local",
    "clock": "24",
    "post_box": "top",
    "notice": "",
    "tags": "note",
    "rate_limit_count": "30",
    "rate_limit_window": "600",
    "login_max_failures": "5",
    "login_lock_minutes": "15",
    "export_every": "daily",
    "export_pdf": "false",
    "export_keep": "7",
    "export_last": "",
}

CHOICES = {
    "post_mode": {"both", "guests", "accounts"},
    "post_box": {"left", "right", "center", "top", "bottom"},
    "time_zone": {"local", "utc"},
    "clock": {"12", "24"},
    "export_every": {"off", "hourly", "daily"},
}

BOOL_KEYS = {
    "users_may_change_display",
    "https_enabled",
    "http_redirect",
    "ntp_enabled",
    "show_ips",
    "guest_name_rules",
    "feature_search",
    "feature_tags",
    "feature_notice",
    "feature_corrections",
    "feature_rate_limit",
    "feature_scheduled_export",
    "feature_display_page",
    "show_number",
    "show_name",
    "show_time",
    "show_ip",
    "show_poster",
    "show_tags",
    "export_pdf",
}

DISPLAY_KEYS = (
    "show_number",
    "show_name",
    "show_time",
    "show_ip",
    "show_poster",
    "show_tags",
    "time_zone",
    "clock",
    "post_box",
)

INT_LIMITS = {
    "https_port": (1, 65535),
    "cert_warn_days": (1, 365),
    "ntp_threshold": (1, 3600),
    "max_entry_length": (1, 2000),
    "rate_limit_count": (1, 10000),
    "rate_limit_window": (1, 86400),
    "login_max_failures": (1, 100),
    "login_lock_minutes": (1, 1440),
    "export_keep": (1, 1000),
}
