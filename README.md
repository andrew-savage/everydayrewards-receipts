# everyday-receipts

Pulls your Everyday Rewards (Woolworths, BWS, Big W, Woolworths Online) e-receipts as PDFs
into a folder, so paperless-ngx can consume them. Runs unattended in Docker, checks for new
receipts every few hours, and never writes the same receipt twice.

There is no official API or email option for e-receipts. This uses the same endpoints the
Everyday Rewards mobile app and website call, after a one-time token capture.

## How it works

1. **One-time token.** You capture the Everyday Rewards **mobile app's** Auth0 login once,
   with an HTTPS proxy, and import it (`import-app-token`). The app is a standard Auth0
   native client, so its refresh token is long-lived. The app never sees your password. The
   token is stored in `data/tokens.json` (mode `0600`).
2. **Refresh chain.** To call the API, the service renews the app's Auth0 access token at
   `auth.everyday.com.au/oauth/token`, then swaps that token for a short-lived API bearer at
   the website's token-exchange endpoint. Both happen automatically; you never touch it again.
3. **Polling.** Every `EDR_POLL_INTERVAL` (default 6 h) it pages through your receipts list,
   newest first, and stops at the first page where every receipt is already saved.
4. **Download.** For each new receipt it fetches the details (store, total, itemised lines)
   and the PDF, then writes the PDF atomically into the output folder as
   `2026-09-13 Woolworths Ivanhoe $44.40 [3f2a9c1e].pdf`. An itemised JSON copy goes to
   `data/json/` (not into the consume folder).

If the app's refresh token is ever rejected, the health check turns unhealthy and
`data/NEEDS_LOGIN` explains why; you capture a fresh token the same way.

## Quick start

```bash
cp .env.example .env            # edit RECEIPTS_DIR / DATA_DIR / PUID / PGID
mkdir -p receipts data
docker compose pull             # prebuilt image from GitHub Container Registry, nothing to compile
docker compose run --rm everyday-receipts import-app-token   # one-time, interactive (see below)
docker compose run --rm everyday-receipts refresh            # proves unattended renewal works
docker compose up -d
docker compose logs -f
```

### Install without cloning

The image is public, so the two files below are all you need on the machine that runs it:

```bash
mkdir -p everyday-receipts && cd everyday-receipts
BASE=https://raw.githubusercontent.com/andrew-savage/everydayrewards-receipts/main
curl -fsSLO $BASE/docker-compose.yml
curl -fsSL  $BASE/.env.example -o .env
mkdir -p receipts data
$EDITOR .env                    # set RECEIPTS_DIR / DATA_DIR / PUID / PGID / TZ
docker compose pull
```

Then follow [Capturing the app token](#capturing-the-app-token-one-time) below.

### Versions

Images are published to GitHub Container Registry for amd64 and arm64:

| Tag | Moves? | Use it when |
| --- | --- | --- |
| `latest` | yes, on every push to `main` | you want the newest build |
| `1.0.0` (exact release) | never | you want a reproducible deployment |
| `1.0` | yes, within that minor series | you want fixes but no breaking changes |

Pin one by setting `EDR_IMAGE_TAG` in `.env`, then `docker compose pull && docker compose up -d`.
Releases are listed at
https://github.com/andrew-savage/everydayrewards-receipts/releases.

### Capturing the app token (one-time)

The mobile app logs in through Auth0. You capture that login once with an HTTPS debugging
proxy, then paste the token response.

1. Install [HTTP Toolkit](https://httptoolkit.com/) (free) on a computer, set your phone to
   use it as a proxy, and trust its CA certificate. HTTP Toolkit's phone setup walks you
   through both.
2. Force a fresh login in the Everyday Rewards app: **log out and back in** (or reinstall).
   A launch of an already-logged-in app also refreshes, but a full login is the surest way to
   see the token.
3. In the proxy, filter for `auth.everyday.com.au` and find the `POST /oauth/token` request.
   Its JSON **response** looks like:

   ```json
   { "access_token": "eyJ...", "refresh_token": "...", "id_token": "eyJ...",
     "scope": "openid offline_access", "expires_in": 86400, "token_type": "Bearer" }
   ```

4. Copy that whole response object. Run
   `docker compose run --rm everyday-receipts import-app-token`, paste it, press Enter.

   The app validates it (loads your receipts) and prints the session summary. Or, if you only
   have the refresh token, `import-app-token --refresh-token 'THE_VALUE'`.

Then run `docker compose run --rm everyday-receipts refresh` to confirm the unattended chain
works, and `docker compose up -d`.

### One-off backfill without a proxy (optional)

If you just want to import your back-catalogue quickly and can't set up a proxy yet, a browser
session works for a single run but **cannot** run unattended (see below). Sign in at
https://www.everyday.com.au, open the developer console (F12 / Cmd-Opt-J), run
`localStorage.getItem('authStatusData')`, copy what it prints, and
`docker compose run --rm everyday-receipts import-session` then paste. Run `once` to pull the
history, but expect `LOGIN REQUIRED` within the hour; switch to an app token for ongoing use.

## Session lifetime (important)

| Source | Lasts | Good for |
| --- | --- | --- |
| Mobile app token (`import-app-token`) | long-lived, renewed automatically | running unattended |
| Browser session (`import-session`) | ~1 hour, **not renewable** | a single backfill |

The Everyday Rewards **website never refreshes its own session** - it logs you in again when
the token expires, and its refresh endpoint rejects the browser refresh token. The mobile app,
by contrast, is a normal Auth0 native client whose refresh token the service can use directly.
That is why unattended running needs the app token.

## paperless-ngx setup

Point paperless-ngx's consume directory at the receipts folder (over NFS) and set:

| paperless-ngx setting | Value | Why |
| --- | --- | --- |
| `PAPERLESS_CONSUMER_POLLING` | `30` | inotify does not fire for files written on another host over NFS; polling does. |
| `PAPERLESS_FILENAME_DATE_ORDER` | `YMD` | Filenames start with `2026-09-13`, so paperless uses the transaction date as the document date. |
| `PAPERLESS_CONSUMER_RECURSIVE` / `PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS` | `true` | Only if you enable `EDR_SUBDIR_BY_PARTNER` to get a `Woolworths` / `BWS` / `Big W` tag per receipt. |

Handy paperless matching rules: a correspondent **Woolworths** with *auto* matching, a
document type **Receipt** matching `Woolworths BWS "Big W"` (*any* algorithm), and a storage
path such as `Receipts/{created_year}/{title}`. The PDFs contain real text (no OCR needed) and
the filename already carries partner, store and amount.

### Sharing the folder via NFS (host side)

The container writes into `RECEIPTS_DIR` on the Docker host; export that directory to the
paperless host. Example `/etc/exports` line:

```
/srv/receipts  192.168.1.20(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000)
```

Set `PUID`/`PGID` in `.env` to the uid/gid that paperless expects to own consumed files, and
make sure the host directory is writable by that uid (`chown 1000:1000 /srv/receipts`).

## Configuration

All settings are environment variables (see `.env.example`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `EDR_OUTPUT_DIR` | `/receipts` | Where PDFs are written (bind-mounted to `RECEIPTS_DIR`). |
| `EDR_DATA_DIR` | `/data` | Tokens, sync state, heartbeat, JSON sidecars. |
| `EDR_JSON_DIR` | `/data/json` | Itemised JSON per receipt; `off` disables. Keep it out of the consume folder. |
| `EDR_POLL_INTERVAL` | `6h` | Sync frequency (`30m`, `6h`, `1d`). |
| `EDR_FULL_SCAN` | `false` | Walk the entire history every run instead of stopping at known receipts. |
| `EDR_MAX_PAGES` | `60` | Safety cap on feed pages per run. |
| `EDR_FILENAME_TEMPLATE` | `{date} {partner} {store} {amount} [{short_id}]` | Fields: `date`, `partner`, `store`, `amount`, `short_id`, `receipt_id`, `id`. |
| `EDR_SUBDIR_BY_PARTNER` | `false` | Write into `Woolworths/`, `BWS/`, ... sub-folders. |
| `EDR_FEED_MODE` | `rest` | `rest` (works with an app token) or `graphql` (web session only). |
| `EDR_LOG_LEVEL` | `INFO` | `DEBUG` logs every request. |
| `EDR_APP_TOKEN_JSON` | – | Non-interactive equivalent of `import-app-token`: the Auth0 token JSON. Used only if no session is stored yet. |
| `EDR_AUTH_STATUS_JSON` | – | Non-interactive equivalent of `import-session` (backfill). |
| `EDR_ACCESS_TOKEN` | – | Static bearer for quick testing only (dies after ~20 min). |

Advanced overrides exist for the hosts and client identifiers in case Woolworths changes
them; defaults are the values the apps ship with: `EDR_API_BASE` (token exchange + receipts),
`EDR_AUTH0_DOMAIN`, `EDR_AUTH0_APP_CLIENT_ID`, `EDR_AUTH0_AUDIENCE`, `EDR_AUTH0_SCOPE`,
`EDR_PARTNER_CLIENT_ID` (token-exchange client), `EDR_REWARDS_CLIENT_ID` (API client),
`EDR_SECURITY_BASE` and `EDR_GRAPHQL_URL` (the web login/GraphQL gateway), `EDR_USER_AGENT`.

## Operating it

```bash
docker compose exec everyday-receipts everyday-receipts status   # session + last sync
docker compose run --rm everyday-receipts once                    # single sync, exit code 0 on success
docker compose logs -f                                            # what it is doing
```

* **Health.** `docker ps` shows `(healthy)` once a sync has succeeded within the last
  `2 × EDR_POLL_INTERVAL + 10 min` and no login is pending.
* **Re-login.** If `data/NEEDS_LOGIN` appears (health `unhealthy`, log line `LOGIN REQUIRED`),
  capture a fresh app token and run `import-app-token` again. Nothing else changes.
* **Check renewal.** `docker compose run --rm everyday-receipts refresh` forces the full
  refresh chain and reports the result.
* **Update.** `docker compose pull && docker compose up -d` picks up the latest published image.
* **Re-download a receipt.** Delete its entry from `data/state.json` (keyed by the stable
  transaction reference) or delete `state.json` entirely; existing PDFs are never overwritten,
  so re-scans are safe.
* **Files vanishing from the consume folder is normal.** paperless-ngx removes each file once
  it has ingested it. The service tracks what it has saved in `data/state.json`, not by
  looking at the folder, so a removed file is never fetched or written again.
* **Backfill.** The first run fetches everything Everyday Rewards still holds (close to three
  years in practice, several hundred receipts). Fuel and points-only activities have no
  e-receipt and are skipped.

## Identity and de-duplication

Receipts are tracked by their **stable transaction reference** (`EEReferenceNumber`, falling
back to `basketKey`), which is also what the `[short_id]` in the filename is derived from.

This matters: the list endpoint's `receiptKey` is re-encrypted with a random salt on every
request (it starts `U2FsdGVkX1`, base64 for `Salted__`), so the *same* receipt has a
different `receiptKey` every time you ask. Keying off it made every sync treat every receipt
as new, re-download the lot and write freshly named files; with a consumer that removes files
after ingesting them, that loops forever. A content hash of each PDF is also recorded as a
second guard, so a receipt already saved is never filed again even if its identity changes.

State written by an earlier version is migrated automatically on first run: old entries are
matched by date, amount and store, so your back-catalogue is not fetched again.

## How the auth actually works

Established by reading the apps' traffic (2026-09):

* The mobile app is an Auth0 native client (`client_id NIG5ul5ubHYy61KoFRNBspUSo1scgDwx`,
  audience `https://www.woolworthsrewards.com.au/auth/`, scope `openid offline_access`). It
  refreshes at `auth.everyday.com.au/oauth/token` with `grant_type=refresh_token`.
* The resulting Auth0 access token (a JWT) is exchanged for an API bearer at
  `POST api.everyday.com.au/wx/v1/rewardspartner/secure/token-exchange` (client
  `eAjOrRlfHIyqpK1KVX8UlmmCFvfmoGXY`), which returns a ~20-minute bearer.
* That bearer drives the plain REST receipt endpoints on `api.everyday.com.au`
  (`.../ereceipts/transactions/list`, `.../details`, `.../details/download`) with client
  `8h41mMOiDULmlLT28xKSv5ITpp3XBRvH`. These have no bot protection.
* The mobile GraphQL host (`prod.mobile-api.woolworths.com.au`) is behind Akamai and is not
  used; the REST endpoints above return the same receipts.
* The website's own session cannot be refreshed, so a browser import is backfill-only.

Auth0 may rotate the refresh token on each refresh; the service persists the new one
immediately. If Auth0 enforces an absolute lifetime, a re-capture will eventually be needed;
`status` and the logs will say so.

Using these private APIs may be against Woolworths' terms; the request volume here is tiny
(one list page and a few downloads every 6 hours), but use at your own risk.

## Development

```bash
uv sync                      # creates .venv with dev deps
uv run pytest                # unit tests (all HTTP is mocked)
uv run everyday-receipts --help
```

To build the image yourself, `docker build -t ghcr.io/andrew-savage/everydayrewards-receipts:latest .`
or uncomment `build: .` in `docker-compose.yml`. CI (`.github/workflows/docker.yml`) builds and
publishes the image on every push to `main`; pushing a tag such as `v0.2.0` also publishes
`0.2.0` and `0.2` tags.

Layout: `config.py` (env settings), `auth.py` (Auth0 refresh + token exchange, token store),
`api.py` (REST + GraphQL client), `models.py` (list items, receipt details), `naming.py`
(dates and filenames), `sync.py` (dedupe + atomic writes), `cli.py`.

## Credits

Endpoint knowledge builds on [T-Fowl/everyday-rewards-receipts](https://github.com/T-Fowl/everyday-rewards-receipts)
and [ekutilov/wooliesR](https://github.com/ekutilov/wooliesR); the Auth0 native-client flow and
token-exchange endpoint were established from the app's and website's own traffic.

## License

MIT, see [LICENSE](LICENSE).
