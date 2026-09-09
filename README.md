# everyday-receipts

Pulls your Everyday Rewards (Woolworths, BWS, Big W, Woolworths Online) e-receipts as PDFs
into a folder, so paperless-ngx can consume them. Runs unattended in Docker, checks for new
receipts every few hours, and never writes the same receipt twice.

There is no official API or email option for e-receipts. This uses the same GraphQL and
REST endpoints the Everyday Rewards website calls, after a one-time browser login.

## How it works

1. **One-time login.** Either sign in on the Everyday Rewards website as usual and hand the
   app the session your browser stored (`import-session`), or let the app drive the same
   Auth0 login the website uses (`login`). The app never sees your password. It keeps the
   resulting bearer and refresh token in `data/tokens.json` (mode `0600`).
2. **Polling.** Every `EDR_POLL_INTERVAL` (default 6 h) it walks the activity feed, newest
   first, and stops at the first page where every receipt is already saved.
3. **Download.** For each new receipt it fetches the receipt details (store, total, itemised
   lines) and the PDF, then writes the PDF atomically into the output folder as
   `2026-09-06 Woolworths Ashfield $90.86 [3f2a9c1e].pdf`. An itemised JSON copy goes to
   `data/json/` (not into the consume folder).
4. **Refresh.** API bearer tokens last about an hour. A **mobile-app** session also carries
   a refresh token good for ~14 months, which the service uses to mint fresh bearers
   unattended. A **browser** session's refresh token lasts only ~2 hours and cannot be
   renewed (see [Session lifetime](#session-lifetime-important)), so it is useful for a
   one-off backfill but not for running unattended. If renewal stops, the health check turns
   unhealthy and `data/NEEDS_LOGIN` explains why.

## Quick start

```bash
cp .env.example .env            # edit RECEIPTS_DIR / DATA_DIR / PUID / PGID
mkdir -p receipts data
docker compose build
docker compose run --rm everyday-receipts import-session   # one-time, interactive (see below)
docker compose run --rm everyday-receipts refresh          # proves unattended renewal works
docker compose up -d
docker compose logs -f
```

### Getting a session (one-time)

**Option A - import the browser session (quickest, for the backfill).** Sign in at
https://www.everyday.com.au in any browser. Open the developer console (F12, or Cmd-Opt-J on
a Mac) and run:

```js
localStorage.getItem('authStatusData')
```

It prints a small JSON blob holding the site's bearer and refresh token. Select and copy
exactly what it printed (the escaped `\"` form, surrounding quotes and Safari's trailing
` = $1` are all fine; the importer unwraps them). Run
`docker compose run --rm everyday-receipts import-session`, paste it, press Enter. The app
verifies it by loading your activity feed and prints how long the refresh token lasts.

Because the web refresh token lives only about two hours, keep the container running: it
renews the session every hour or so. If it is stopped for longer than the refresh token's
lifetime, run `import-session` again.

**Option B - `login`.** `docker compose run --rm everyday-receipts login` asks the
Everyday Rewards backend for its Auth0 login URL (the same one the "Log in" button uses)
and prints it. Sign in, and you land on `https://www.everyday.com.au/callback?code=...`.
Paste that **full address** back into the terminal; the app swaps the code for tokens
through the site's own token endpoint.

The catch: the callback page runs JavaScript that uses the code itself within a second.
Block JavaScript for `www.everyday.com.au` in your browser *before* signing in (Chrome:
Settings → Privacy and security → Site settings → JavaScript → *Not allowed* → add
`www.everyday.com.au`; remove it afterwards). The Auth0 page is on another host and keeps
working. `login --redirect-uri http://localhost:8765/callback --listen 0.0.0.0:8765`
would avoid all that, but Woolworths' Auth0 client only allows its own callback URL; the
command checks and tells you up front if a redirect URI is rejected.

Whichever option you use, finish with `everyday-receipts refresh`. It forces a token renewal
and confirms the unattended path works before you rely on it.

## Session lifetime (important)

There are two kinds of session, and they behave very differently:

| Source | Refresh token lasts | Good for |
| --- | --- | --- |
| Browser (`import-session` of `authStatusData`) | ~2 hours, **not renewable** | one-off backfill of your history |
| Mobile app (captured refresh token) | ~14 months, renewed automatically | running unattended |

The Everyday Rewards **website never refreshes its own session** - it makes you log in again
when the token expires - and its refresh token is rejected by the refresh endpoint. So a
browser session cannot be kept alive: after about an hour the container will report
`LOGIN REQUIRED`. That is fine for the initial import of your back-catalogue, which finishes
in minutes.

For unattended running you need the **mobile app's** long-lived refresh token. You capture it
once with an HTTPS debugging proxy on your phone:

1. Install [HTTP Toolkit](https://httptoolkit.com/) (free) on a computer, or mitmproxy, and
   set your phone to use it as a proxy with its CA certificate trusted. HTTP Toolkit's
   Android/iOS setup walks you through both.
2. Open the Everyday Rewards app and log in (or just open it if already logged in - it
   refreshes on launch).
3. In the proxy, find a request to `api-wr.com` or `woolworthsrewards.com.au` whose JSON
   response contains `"refresh"` and `"refreshExpiredInSeconds"` (a big number like
   `38879999`). Copy the `refresh` value, and note `refreshExpiredInSeconds`.
4. Import it:

   ```bash
   docker compose run --rm everyday-receipts import-session \
     --refresh-token 'PASTE_THE_REFRESH_VALUE' --refresh-lifetime 38879999
   ```

   The app mints a bearer from it immediately and prints the lifetime. Then
   `docker compose up -d` and it runs on its own, renewing every hour or so for ~14 months.

If the mobile refresh token is ever rejected, capture a fresh one the same way. If the
capture also includes the request's `client_id` header and it differs from the default, set
`EDR_REWARDS_CLIENT_ID` to match.

## paperless-ngx setup

Point paperless-ngx's consume directory at the receipts folder (over NFS) and set:

| paperless-ngx setting | Value | Why |
| --- | --- | --- |
| `PAPERLESS_CONSUMER_POLLING` | `30` | inotify does not fire for files written on another host over NFS; polling does. |
| `PAPERLESS_FILENAME_DATE_ORDER` | `YMD` | Filenames start with `2026-09-06`, so paperless uses the transaction date as the document date. |
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
| `EDR_LOG_LEVEL` | `INFO` | `DEBUG` logs every request. |
| `EDR_APIGEE_REFRESH_BODY_KEY` | `refresh_token` | Preferred JSON key for the refresh call; the other spelling is tried automatically and the working one remembered. |
| `EDR_LOGIN_REDIRECT_URI` | `https://www.everyday.com.au/callback` | Redirect URI requested by `login`. |
| `EDR_ACCESS_TOKEN` | – | Static bearer for quick testing only (dies after ~30 min). |
| `EDR_AUTH_STATUS_JSON` | – | Non-interactive equivalent of `import-session`, used only if no session is stored yet. |

Advanced overrides exist for the API hosts and client identifier (`EDR_API_BASE`,
`EDR_SECURITY_BASE`, `EDR_GRAPHQL_URL`, `EDR_REWARDS_CLIENT_ID`, `EDR_USER_AGENT`) in case
Woolworths changes them; defaults are the values the public web app ships with.
`EDR_SECURITY_BASE` (default `https://apigee-prod.api-wr.com`) is the host for login and
token refresh: those routes respond only on the direct apigee gateway, not on the
Akamai-fronted `api.everyday.com.au` alias, where they hang.

## Operating it

```bash
docker compose exec everyday-receipts everyday-receipts status   # session + last sync
docker compose run --rm everyday-receipts once                    # single sync, exit code 0 on success
docker compose logs -f                                            # what it is doing
```

* **Health.** `docker ps` shows `(healthy)` once a sync has succeeded within the last
  `2 × EDR_POLL_INTERVAL + 10 min` and no login is pending.
* **Re-login.** If `data/NEEDS_LOGIN` appears (health `unhealthy`, log line `LOGIN REQUIRED`),
  run `import-session` or `login` again. Nothing else needs to change.
* **Check renewal.** `docker compose run --rm everyday-receipts refresh` forces a token
  refresh and reports the result.
* **Re-download a receipt.** Delete its entry from `data/state.json` (keyed by receipt id) or
  delete `state.json` entirely; existing PDFs are never overwritten, so re-scans are safe.
* **Backfill.** The first run fetches everything Everyday Rewards still holds (close to three
  years in practice, several hundred receipts). Fuel and points-only activities have no
  e-receipt and are skipped.

## Known unknowns

This talks to an unofficial API, so a few things can only be confirmed with a real session:

* **Web sessions cannot be renewed.** Confirmed 2026-09: neither the current site nor the
  old one refreshes its session (both log out on expiry), and the refresh endpoint rejects a
  browser refresh token (`1013 Invalid Refresh Token`). Unattended running therefore needs a
  mobile-app token (see [Session lifetime](#session-lifetime-important)).
* **Mobile token endpoint details.** The mobile refresh token is expected to work with
  `/wx/v2/security/refreshToken` on the apigee gateway (that is the app's own endpoint), but
  the exact `client_id` header the app uses was not captured; override `EDR_REWARDS_CLIENT_ID`
  if a captured token is rejected with the default.
* **Refresh request body.** The website exposes `/wx/v2/security/refreshToken` but never
  calls it, so the JSON key is inferred. The app tries `refresh_token` then `refreshToken`
  and remembers whichever the endpoint accepts; a rejected token (401/403) means a re-login.
* **Auth0 client.** The site's JavaScript also carries a newer Auth0 SPA client, but Auth0
  rejects it with *Callback URL mismatch* for the production callback, so the app uses the
  backend-mediated flow the live site uses.

Using the site's private API may be against Woolworths' terms; the request volume here is
tiny (one feed page and a few downloads every 6 hours), but use at your own risk.

## Development

```bash
uv sync                      # creates .venv with dev deps
uv run pytest                # unit tests (all HTTP is mocked)
uv run everyday-receipts --help
EDR_DATA_DIR=./data EDR_OUTPUT_DIR=./receipts uv run everyday-receipts login
```

Layout: `config.py` (env settings), `auth.py` (login flow, token store, refresh),
`api.py` + `queries.py` (GraphQL/REST client), `models.py` (feed items, receipt details),
`naming.py` (dates and filenames), `sync.py` (dedupe + atomic writes), `cli.py`.

## Credits

Endpoint knowledge builds on [T-Fowl/everyday-rewards-receipts](https://github.com/T-Fowl/everyday-rewards-receipts)
and [ekutilov/wooliesR](https://github.com/ekutilov/wooliesR); the login-url and token
endpoints were taken from the public JavaScript of www.everyday.com.au.

## License

MIT, see [LICENSE](LICENSE).
