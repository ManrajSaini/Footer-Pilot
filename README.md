# FooterPilot

Company-wide email signature manager for Google Workspace. Product description: `idea.md`. Build plan: `plan.md`. Deferred ideas: `later.md`.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Docker (for the local Postgres database)

## First-time setup

```sh
uv sync                              # install dependencies into .venv
cp .env.example .env                 # optional: the defaults work for local development
docker compose up -d                 # start Postgres (creates dev and test databases)
uv run python -m app.cli migrate     # create tables and the job queue schema
uv run python scripts/tailwind.py build   # build app/static/css/app.css
```

## Everyday commands

| Task | Command |
|---|---|
| Run the web app (http://127.0.0.1:8000) | `uv run python -m app.cli web --reload` |
| Run the background worker | `uv run python -m app.cli worker` |
| Queue a test job (the worker logs it) | `uv run python -m app.cli ping` |
| Apply migrations | `uv run python -m app.cli migrate` |
| Create a migration after changing models | `uv run alembic revision --autogenerate -m "describe change"` |
| Rebuild CSS while editing templates | `uv run python scripts/tailwind.py watch` |
| Run tests | `uv run pytest` |
| Lint and format | `uv run ruff check .` and `uv run ruff format .` |

The health check is at `/healthz` (returns 503 when the database is unreachable).

## Background worker

Directory sync runs in the worker, not in the web process. Keep `uv run python -m app.cli worker` running in a second terminal, otherwise "Sync now" and the wizard's last step wait until the page reports that the sync did not finish.

While it runs, the worker also acts on its own schedule (times in UTC):

- **Every hour** it reads each connected organisation's Google directory. If a signature has been applied to everyone, new hires and people whose details changed get it straight after.
- **Every day at 03:30** it re-applies the live signature to every included mailbox, for organisations with that setting on (Settings page, on by default).

With `GOOGLE_MODE=real` these write to real mailboxes without anyone clicking anything. Stop the worker when you do not want that.

## Applying a signature

On the Signature page, "Apply to me only" writes the current draft to your own mailbox at once. "Apply to everyone" makes the draft live and queues one job per included employee; the progress page follows it. Both overwrite whatever signature the mailbox had, and FooterPilot cannot restore the old one.

Images only load in real email when they are hosted publicly (`STORAGE_BACKEND=r2` and a public `ASSET_BASE_URL`). With local storage the designer shows a warning: text and links work, but logos, photos and social icons appear broken.

### What Google does not let an app control

Both were checked against Google's Gmail API reference and on the real test domain
(2026-11-20). FooterPilot sets the signature text; these two things belong to Gmail.

- **Where the signature sits in a reply or forward.** By default Gmail puts it below
  the earlier messages, where it is hidden behind "show trimmed content" and piles up in
  long threads, and adds a "--" line above it. Each person fixes all of this with one
  tick box in Gmail: Settings > General > Signature > "Insert signature before quoted
  text in replies and remove the '--' line that precedes it" (it covers forwards too).
  There is no API field or admin setting for it.
  The app shows a ready-to-send note with these steps after applying and on Settings.
- **Employees can edit their signature.** Google has no lock. The daily re-apply puts
  the company signature back.

The products that avoid the first limit do it by routing all outgoing mail through
their own servers, or with a browser extension. See `later.md`.

### Checks on a real Workspace domain

To be run by hand against the test domain, with results recorded here.

| Check | Result |
|---|---|
| "Apply to me only" writes the signature to the admin's real mailbox | Works (2026-11-20, Classic layout) |
| User with no existing signature: the new one appears and is used for new mail | Yes (2026-11-20, a newly created user) |
| "Apply to everyone" updates every included mailbox and the summary matches | Yes (2026-11-20, 3 mailboxes) |
| User with one existing signature: it is replaced | Yes, visible after the person reloads Gmail (2026-11-20) |
| Excluding someone who already has the company signature | Removes it from their Gmail; including them again applies the current one (2026-11-20) |
| User with several signatures saved in Gmail: which one is used for new mail | The company signature is added as a new one and becomes the default for new mail. Their own saved signatures are kept, not replaced (2026-11-20) |
| Signature appears on forwards | Yes, but below the forwarded message until the person ticks the Gmail setting above (2026-11-20) |
| Signature appears on replies | Yes, but below the quoted thread until the person ticks the Gmail setting above (2026-11-20) |
| Gmail mobile app shows the signature | Yes (2026-11-20) |
| Layout, links and images in mail received by Gmail | Partly (2026-11-20): layout and text are correct in a received message. Images did not load, as expected with local storage. Links not checked |
| Layout, links and images in mail received by Outlook | not yet run |
| Layout, links and images in mail received by Apple Mail | not yet run |
| New user added in Google Admin gets a signature after the next sync | Yes, with no admin action, seconds after the sync (2026-11-20) |
| Job title changed in Google Admin changes that signature after the next sync | Yes (2026-11-20) |

## Banner and click counting

The Banner page puts one clickable image under everyone's signature. Upload an image (PNG, JPEG or GIF, up to 2 MB, shown at up to 600 px wide), say where it leads, add a description, check the preview and activate it. The description is required: it is what readers see when their mail program blocks images. Activating, replacing or removing a banner updates every included mailbox, with the same confirmation and progress page as "Apply to everyone". Earlier banners stay listed with their clicks.

With click counting on (Settings, on by default), the banner, website and social links in a signature point at `{TRACKING_BASE_URL}/c/<token>?e=<employee id>`. That address records the click and redirects to the real destination. Email and phone links are never changed. The Analytics page shows clicks per day and per link for the last 7, 30 or 90 days.

- A click row stores the link, the time and whose signature it was. No IP address and no user agent is kept.
- Mail security scanners and link previewers that identify themselves are stored but not counted. Some do not identify themselves, so the numbers are a guide.
- Changing a link's destination, or replacing the banner, creates a new counted link. Old ones keep redirecting, so links in emails already sent keep working.
- Switching click counting off puts the real addresses back in every signature.

**On a development machine** `TRACKING_BASE_URL` defaults to `APP_BASE_URL`, usually `http://127.0.0.1:8000`. Counted links in real email then only open on that machine. Before applying to real mailboxes from a development machine, switch click counting off in Settings (the pages warn about this), or set `TRACKING_BASE_URL` to a public address that reaches the app.

Checks on the real test domain. Those marked as run were done from a development machine, where counted links only open on that machine and images are not hosted; the rest need a public address and hosted images.

| Check | Result |
|---|---|
| Activating, replacing and removing a banner updates every included mailbox | Yes (2026-11-21, 4 mailboxes) |
| An activated banner appears in a received email, with its image | Partly (2026-11-21): the banner is there and clickable, shown as its description. The image did not load, as expected with local storage |
| Clicking the banner in a received email opens its target | Yes, on the development machine (2026-11-21) |
| The click appears on the Analytics page | Yes (2026-11-21) |
| The website link in a received email opens the right page and is counted | Yes, on the development machine (2026-11-21) |
| Switching click counting off puts the real addresses back | Yes (2026-11-21) |
| Banner image loads in a received email | not yet run |
| Clicks from another device or another person are counted | not yet run |

A click made while writing a message (on the signature in the draft) is counted like any other: it is the same link, and the request does not say where it was clicked.

## Signing in during development

Open the app at the address in `APP_BASE_URL` (default `http://127.0.0.1:8000`), not `localhost`: the session cookie and Google's redirect are tied to that exact host.

**Without Google (fake mode).** Set `GOOGLE_MODE=fake` and `DEV_LOGIN=true` in `.env`, then open `/dev/login`. The part of the address after `@` becomes the company. The part before `@` decides how the connection check behaves:

| Address starts with | Behaviour |
|---|---|
| `nodelegation` | Delegation has not been added in the Admin console |
| `nogmail` | Delegation is missing the Gmail scope |
| `notadmin` | The signed-in user is not a Workspace admin |
| `outage` | Google is unreachable |
| anything else | Everything works |

**With real Google.** Set `GOOGLE_MODE=real` and fill in `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_SERVICE_ACCOUNT_JSON` and `GOOGLE_DWD_CLIENT_ID`. The OAuth client's authorised redirect URI must be `{APP_BASE_URL}/auth/google/callback`. While the Google app is in Testing mode, each person who signs in must be listed as a test user. Then sign in at `/signup` with a Workspace super admin and follow the wizard.

## Notes

- **Tests** use the `footerpilot_test` database from docker-compose and an in-memory job queue. They never call a real external service. Postgres must be running.
- **Database host:** use `127.0.0.1`, not `localhost`. On Windows, `localhost` tries IPv6 first and each connection stalls for seconds against Docker's port mapping.
- **Windows:** always start the app through `python -m app.cli`. It selects an event loop that the Postgres driver supports; plain `uvicorn app.main:create_app` does not.
- **CSS:** `app/static/css/app.css` is generated and not committed. The build script downloads the Tailwind standalone CLI into `.tools/`. If that download is blocked and Node is installed, it installs the same Tailwind version from npm instead.
- **Vendored scripts:** HTMX 2.0.11 and Alpine.js 3.17.5 live in `app/static/vendor/`. To upgrade, replace the files and update the two `<script>` tags in `app/templates/base.html`.
- **Signature templates:** the email-safe HTML templates are in `app/signatures/templates/`. Tests pin their exact output in `tests/signatures/golden/`; after an intentional change run `UPDATE_GOLDEN=1 uv run pytest tests/signatures/test_render.py` and review the diff.
- **Social icons:** signatures link to `{ASSET_BASE_URL}/static/social/<network>.png`. On a local machine that URL is not reachable from Gmail, so icons appear broken when a locally generated signature is pasted into a real mailbox. See `app/static/social/README.md` for where the icons come from.
- **Trying things without touching your data:** all workers on one database share a job queue. To experiment in fake mode while real data is in your development database, point a second copy of the app at its own database, port and media folder: set `DATABASE_URL`, `APP_BASE_URL`, `MEDIA_ROOT`, `GOOGLE_MODE=fake` and `DEV_LOGIN=true` for that run, and migrate that database first.
- **Image storage:** `STORAGE_BACKEND=local` (the default) writes to `var/media` and serves it at `/media`; `STORAGE_BACKEND=r2` uploads to Cloudflare R2 and needs the five `R2_*` settings. Employee photos are re-encoded to 160 px square JPEGs before they are stored.
- **Networks that inspect TLS:** on a network whose proxy re-signs HTTPS traffic with its own certificate, uploads to R2 fail with `CERTIFICATE_VERIFY_FAILED`. Do not disable verification. Use local storage on that network, or test R2 from another one.
- **Upgrading Procrastinate:** `migrate` installs its schema only once. When bumping the Procrastinate version, apply the SQL migration scripts listed in its release notes.
- **Production settings:** with `ENV=production` the app refuses to start if `GOOGLE_MODE=fake`, `DEV_LOGIN=true`, `STORAGE_BACKEND=local` or `SECRET_KEY` is unset.

## Docker image

One image serves both processes.

```sh
docker build -t footerpilot .
docker run -e DATABASE_URL=... -p 8000:8000 footerpilot                      # web
docker run -e DATABASE_URL=... footerpilot python -m app.cli worker          # worker
docker run -e DATABASE_URL=... footerpilot python -m app.cli migrate         # migrations
```

## Layout

```
app/
  main.py        app factory
  cli.py         web / worker / migrate / ping commands
  config.py      settings from environment variables
  db.py          engine and session dependency
  models/        SQLAlchemy models
  web/           routers, access rules, CSRF, rate limit, error pages, template setup
  services/      business logic (sign-in, connecting a domain, directory sync, images,
                 applying signatures, banners, click counting, analytics)
  google/        gateways to Google: interfaces, real and fake implementations
  storage/       public image storage: local disk or Cloudflare R2
  signatures/    design schema, renderer and email-safe templates
  tasks/         background jobs (Procrastinate)
  templates/     Jinja pages
  static/        CSS, vendored JS
alembic/         database migrations
scripts/         build helpers
tests/
```
