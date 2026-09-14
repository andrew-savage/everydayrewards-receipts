# Changelog

## 1.0.0 - 2026-09-14

First published release. Runs unattended against the Everyday Rewards mobile-app session and
files e-receipt PDFs for paperless-ngx.

### Added
- **Mobile app token auth** (`import-app-token`), the only session that can run unattended.
  The service renews the app's Auth0 access token at `auth.everyday.com.au/oauth/token` and
  exchanges it for an API bearer, with no further interaction.
- REST receipts feed (`transactions/list` / `details` / `details/download`), which is what
  accepts an app-derived bearer. `EDR_FEED_MODE=graphql` keeps the old web-session path.
- `refresh` command to verify the whole renewal chain on demand.
- Prebuilt multi-architecture images on GitHub Container Registry, and `EDR_IMAGE_TAG` for
  pinning a version.
- Itemised JSON sidecar per receipt, atomic writes, health check, and `data/NEEDS_LOGIN`
  when a session genuinely needs replacing.

### Fixed
- **Receipts were re-downloaded forever.** The list endpoint's `receiptKey` is re-encrypted
  with a random salt on every request, so using it as identity made every sync treat every
  receipt as new. With a consumer that removes files after ingesting them, this never
  settled. Receipts are now tracked by their stable transaction reference, with a PDF
  content hash as a second guard. Existing state migrates automatically.
- Multi-line pasted JSON is accepted when importing a token.
- `refresh` reports the token that actually keeps the session alive.

### Known limitations
- A browser session (`import-session`) cannot be renewed; it is for a one-off backfill only.
- Whether the app's Auth0 refresh token has an absolute expiry is not yet known. If it ever
  lapses, capture a fresh token and re-run `import-app-token`.
