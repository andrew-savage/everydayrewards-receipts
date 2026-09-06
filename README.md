# everyday-receipts

Pulls your Everyday Rewards (Woolworths, BWS, Big W, Woolworths Online) e-receipts as PDFs
into a folder, so paperless-ngx can consume them. Runs unattended in Docker, checks for new
receipts every few hours, and never writes the same receipt twice.

There is no official API or email option for e-receipts. This uses the same GraphQL and
REST endpoints the Everyday Rewards website calls, after a one-time browser login.

## How it works

1. **One-time login.** You log in through Woolworths' normal Auth0 login page (email,
   password, one-time code, passkey - whatever your account needs). The app never sees your
   password. It keeps the resulting refresh token in `data/tokens.json` (mode `0600`).
2. **Polling.** Every `EDR_POLL_INTERVAL` (default 6 h) it walks the activity feed, newest
   first, and stops at the first page where every receipt is already saved.
3. **Download.** For each new receipt it fetches the receipt details (store, total, itemised
   lines) and the PDF, then writes the PDF atomically into the output folder as
   `2026-09-06 Woolworths Ashfield $90.86 [3f2a9c1e].pdf`. An itemised JSON copy goes to
   `data/json/` (not into the consume folder).
4. **Refresh.** API bearer tokens last about 30 minutes; they are refreshed automatically
   from the stored refresh token. If that ever stops working, the container's health check
   turns unhealthy and a `data/NEEDS_LOGIN` file explains why.

## Quick start

```bash
cp .env.example .env            # edit RECEIPTS_DIR / DATA_DIR / PUID / PGID
mkdir -p receipts data
docker compose build
docker compose run --rm everyday-receipts login     # one-time, interactive
docker compose up -d
docker compose logs -f
```

### The login step

`login` prints an Auth0 URL. Open it in any browser, sign in, and you land on
`https://www.everyday.com.au/callback?code=...&state=...`. Paste that **full address** back
into the terminal. The app exchanges the code itself; the code is single-use and short-lived.

The Everyday Rewards callback page runs JavaScript that tries to use the code too and then
navigates away, so you have a few seconds to copy the address bar. Two ways to make this
painless:

* **Block JavaScript for `www.everyday.com.au` while you log in** (Chrome: Settings →
  Privacy and security → Site settings → JavaScript → *Not allowed to use JavaScript* → Add
  `www.everyday.com.au`). The callback page then stays put with the code in the address bar.
  Remove the rule afterwards. The Auth0 login page is on a different host and is unaffected.
* **Use a local redirect instead.** Run
  `docker compose run --rm -p 8765:8765 everyday-receipts login --redirect-uri http://localhost:8765/callback --listen 0.0.0.0:8765`
  and the app receives the redirect itself. This only works if Woolworths' Auth0 client
  allows a localhost callback; if Auth0 shows *Callback URL mismatch*, fall back to the
  first method. (Without `--listen` you can also just copy the failed `localhost` URL from
  the address bar and paste it.)

When login succeeds the app prints the token lifetimes and how many receipts are on the
first page of your activity feed.

### Alternative: import the browser session

If the PKCE login gives trouble, log in on https://www.everyday.com.au in a browser, open the
developer console and run:

```js
copy(localStorage.getItem('authStatusData'))
```

then `docker compose run --rm everyday-receipts import-session` and paste. This imports the
site's bearer and refresh token and renews them through the API's refresh endpoint. Note the
caveat in [Known unknowns](#known-unknowns).

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
| `EDR_FULL_SCAN` | `false` | Walk the entire 14-month history every run instead of stopping at known receipts. |
| `EDR_MAX_PAGES` | `60` | Safety cap on feed pages per run. |
| `EDR_FILENAME_TEMPLATE` | `{date} {partner} {store} {amount} [{short_id}]` | Fields: `date`, `partner`, `store`, `amount`, `short_id`, `receipt_id`, `id`. |
| `EDR_SUBDIR_BY_PARTNER` | `false` | Write into `Woolworths/`, `BWS/`, ... sub-folders. |
| `EDR_LOG_LEVEL` | `INFO` | `DEBUG` logs every request. |
| `EDR_APIGEE_REFRESH_BODY_KEY` | `refresh_token` | JSON key used by the `import-session` refresh call (see below). |
| `EDR_ACCESS_TOKEN` | – | Static bearer for quick testing only (dies after ~30 min). |
| `EDR_AUTH_STATUS_JSON` | – | Non-interactive equivalent of `import-session`, used only if no session is stored yet. |

Advanced overrides exist for the API hosts and client identifiers (`EDR_API_BASE`,
`EDR_GRAPHQL_URL`, `EDR_REWARDS_CLIENT_ID`, `EDR_PARTNER_CLIENT_ID`, `EDR_AUTH0_*`,
`EDR_USER_AGENT`) in case Woolworths changes them; defaults are the values the public web app
ships with.

## Operating it

```bash
docker compose exec everyday-receipts everyday-receipts status   # session + last sync
docker compose run --rm everyday-receipts once                    # single sync, exit code 0 on success
docker compose logs -f                                            # what it is doing
```

* **Health.** `docker ps` shows `(healthy)` once a sync has succeeded within the last
  `2 × EDR_POLL_INTERVAL + 10 min` and no login is pending.
* **Re-login.** If `data/NEEDS_LOGIN` appears (health `unhealthy`, log line `LOGIN REQUIRED`),
  run `docker compose run --rm everyday-receipts login` again. Nothing else needs to change.
* **Re-download a receipt.** Delete its entry from `data/state.json` (keyed by receipt id) or
  delete `state.json` entirely; existing PDFs are never overwritten, so re-scans are safe.
* **Backfill.** The first run fetches everything Everyday Rewards still holds (about 14
  months). Fuel and points-only activities have no e-receipt and are skipped.

## Known unknowns

This talks to an unofficial API, so a few things can only be confirmed with a real session:

* **Auth0 refresh token lifetime.** The app refreshes as often as needed, so inactivity
  limits are not a concern, but Auth0 tenants can set an absolute lifetime. If a re-login is
  ever demanded, `status` and the logs will say so.
* **`import-session` refresh call.** The website exposes `/wx/v2/security/refreshToken` but
  never calls it, so the request body is inferred (`{"refresh_token": ...}`, configurable via
  `EDR_APIGEE_REFRESH_BODY_KEY`). Prefer the `login` command, which uses documented OAuth.
* **Callback URL for a local redirect.** Whether Auth0 accepts `http://localhost:...` was not
  probed; the copy-the-address-bar method always works.

Using the site's private API may be against Woolworths' terms; the request volume here is
tiny (one feed page and a few downloads every 6 hours), but use at your own risk.

## Development

```bash
uv sync                      # creates .venv with dev deps
uv run pytest                # unit tests (all HTTP is mocked)
uv run everyday-receipts --help
EDR_DATA_DIR=./data EDR_OUTPUT_DIR=./receipts uv run everyday-receipts login
```

Layout: `config.py` (env settings), `auth.py` (PKCE login, token store, refresh),
`api.py` + `queries.py` (GraphQL/REST client), `models.py` (feed items, receipt details),
`naming.py` (dates and filenames), `sync.py` (dedupe + atomic writes), `cli.py`.

## Credits

Endpoint knowledge builds on [T-Fowl/everyday-rewards-receipts](https://github.com/T-Fowl/everyday-rewards-receipts)
and [ekutilov/wooliesR](https://github.com/ekutilov/wooliesR); the Auth0 login flow and
token-exchange endpoint were taken from the public JavaScript of www.everyday.com.au.

## License

MIT, see [LICENSE](LICENSE).
