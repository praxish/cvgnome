# Reporting a security issue

Do not post personal career data, API keys, raw vaults, or exploit details in a
public issue. Use GitHub's private vulnerability-reporting form for this project:

https://github.com/praxish/cvgnome/security/advisories/new

Include the affected release and platform, a concise impact description, and a
minimal reproduction using synthetic data. If the private form is unavailable,
open an issue that only requests a private reporting channel; do not include the
sensitive details. There is no guaranteed response time or paid support service.

The latest published release is the supported security-fix target. Earlier
releases may require upgrading. See release notes for supported platforms.

CVGnome stores career material on the user's computer. Local filesystem
permissions are not application-level encryption; protect the device and its
backups. Optional provider features transmit the described data directly to the
selected provider. See docs/PRIVACY.md for data flow and retention boundaries.

For maintainers: enable GitHub private vulnerability reporting before public
release. Review fixes privately when disclosure could harm users, and publish
an advisory and upgrade guidance when a security fix is released.
