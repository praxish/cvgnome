# SPDX-License-Identifier: MPL-2.0
"""Read-only identifiers for explicit migration from the earlier application.

No runtime path automatically discovers, opens, changes, or removes this data.
The operator supplies the old directory to the offline migration command.
"""

LEGACY_APP_IDENTIFIER = "com.careervault.desktop"
LEGACY_DATABASE_FILENAME = "careervault.sqlite3"
LEGACY_ENGINE_PACKAGE = "careervault_engine"

# Exclude links and network labels embedded by earlier software from public
# profile projections and resumes; these are never current app branding.
LEGACY_PROFILE_HOSTS = ("careervault.praxish.com", "resumaid.praxish.com")
LEGACY_PROFILE_NETWORKS = ("careervault", "resumaid")
