# FooterPilot V1 — Implementation Plan

This is the build plan for V1 of FooterPilot (product description: `idea.md`). It is written for a coding assistant to implement phase by phase. V2 ideas live in `later.md` and must not be built now.

## 1. How to use this plan

- Build the phases in order. Each phase ends in a working, demoable state with a "Done when" checklist. Do not start a phase until the previous one passes its checklist.
- Stay inside the scope of the current phase. If something seems missing, check section 3 (scope cuts) and `later.md` before adding it.
- Sections 4 to 7 (stack, architecture, data model, conventions) apply to every phase.
- Library APIs named here (Procrastinate, Razorpay, Google) are described from memory. Check the current official docs before coding against them, and follow the docs where they differ from this plan.
- Section 17 lists things only the human owner can do (accounts, keys, a test Workspace domain). Everything must run locally without them using fake mode (section 5.3).

## 2. What V1 is

A web app where a Google Workspace admin signs in, authorises FooterPilot once for the whole domain, designs one company signature from a template, and pushes it to every employee's Gmail. Employee details come from the Google directory and stay in sync automatically. A clickable promotional banner can be added, and clicks are counted. Small teams use it free with a "Powered by FooterPilot" link; larger teams pay per user per month. A public, no-sign-up signature generator brings in search traffic.

## 3. Scope cuts versus idea.md

These are deliberate. Do not build the fuller version.

| idea.md says | V1 builds |
|---|---|
| Drag-and-drop editor | 4 templates plus a form-based side panel with live preview |
| Signatures for the company | Exactly one signature per organisation, applied to all included employees |
| Admin access | One admin per organisation (the person who connects the domain) |
| Instant automatic updates | Hourly directory polling plus a manual "Sync now" button |
| All email addresses | Each employee's primary Gmail address only (no aliases) |
| Guided setup with video | Wizard with copy buttons and screenshot/video placeholders |
| Live setup help | A "Book a setup call" link from config |
| Click tracking | Aggregate counts per link, with a basic bot filter |

FooterPilot sends no email of its own in V1 (no notifications, no receipts beyond what Razorpay sends).

## 4. Tech stack (decided)

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| Packaging | uv, `pyproject.toml` |
| Web framework | FastAPI, served by uvicorn |
| UI | Jinja2 server-rendered pages, HTMX, Alpine.js, Tailwind CSS (standalone CLI, no Node) |
| Database | PostgreSQL 16+ |
| ORM and migrations | SQLAlchemy 2.x (async) with Alembic |
| DB driver | psycopg 3 for both SQLAlchemy (`postgresql+psycopg://`) and Procrastinate |
| Background jobs | Procrastinate (Postgres-backed queue, periodic tasks) |
| Settings | pydantic-settings, environment variables, `.env` in dev |
| Admin sign-in | Google OpenID Connect via Authlib |
| Google APIs | `google-auth` and `google-api-python-client`, service account with domain-wide delegation |
| Image storage | Cloudflare R2 through boto3 (S3 API), public custom domain |
| Image processing | Pillow |
| Payments | Razorpay Subscriptions (`razorpay` Python SDK, Checkout.js) |
| Hosting | One Docker image on Render: web service, worker service, managed Postgres |
| Quality | ruff (lint and format), pytest, pytest-asyncio, httpx2 test client |
| Errors | Sentry SDK, enabled only when `SENTRY_DSN` is set |

HTMX and Alpine.js are vendored into `app/static/vendor/`, not loaded from a CDN.

## 5. Architecture

### 5.1 Processes

- **web**: FastAPI app. Serves pages, HTMX fragments, the click redirect and the Razorpay webhook.
- **worker**: Procrastinate worker. Runs directory sync, signature deployment and periodic tasks.
- **Postgres**: application data and the job queue.

Both processes are the same Docker image with different start commands.

### 5.2 Google integration model

V1 uses a **service account with domain-wide delegation (DWD)**, not a Marketplace app and not per-user OAuth tokens.

- FooterPilot owns one Google Cloud project with one service account. Its key is an environment secret. No customer tokens are stored.
- The customer's super admin opens the Admin console (Security > Access and data control > API controls > Domain-wide delegation, `https://admin.google.com/ac/owl/domainwidedelegation`), adds FooterPilot's service-account **Client ID** and these two scopes:
  - `https://www.googleapis.com/auth/admin.directory.user.readonly` (read employee names, titles, phones, photos)
  - `https://www.googleapis.com/auth/gmail.settings.basic` (write the signature setting)
- To read the directory, the service account impersonates the admin (`subject = admin email`). To write a signature, it impersonates that employee (`subject = employee email`) and calls `users.settings.sendAs.patch` with `userId="me"`, `sendAsEmail=<primary email>` and body `{"signature": html}`.
- Neither scope can read, send or delete mail. This is the basis of the "we never read your emails" claim; keep it true.

Admin sign-in is a separate, ordinary "Sign in with Google" flow requesting only `openid email profile`.

### 5.3 Gateways and fake mode

All Google calls go through two interfaces in `app/google/`:

- `DirectoryGateway`: `verify_admin(admin_email)`, `list_users(admin_email)`, `get_photo(admin_email, user_id)`
- `GmailGateway`: `verify_access(email)`, `set_signature(email, html)`

Each has a real implementation and a fake one, selected by `GOOGLE_MODE=real|fake`. The fake directory serves about 12 employees from a JSON fixture (including one suspended user, one with no title or phone, one with a very long name). The fake Gmail records writes in memory or a dev table and can be told to fail for chosen addresses. With `ENV=dev` and `DEV_LOGIN=true`, a `/dev/login` route signs in a fake admin without Google. Both must be impossible to enable when `ENV=production` (fail at startup).

The Google client libraries are synchronous. Call them through `asyncio.to_thread` inside the real gateways.

Storage follows the same pattern: `Storage` interface with `r2` and `local` backends (`local` writes to `./var/media` and serves it at `/media`).

### 5.4 Signature rendering

- A design is a validated Pydantic model (`SignatureDesign`, section 6.2) stored as JSONB.
- `render_signature(design, employee, org_ctx) -> str` in `app/signatures/render.py` is a **pure function**: no database, no network, no clock. `org_ctx` carries everything else it needs: plan (for the Powered-by link), the active banner, the link map (target URL to tracking URL) and asset URLs.
- Templates are Jinja2 files in `app/signatures/templates/` rendered in a separate sandboxed environment with autoescape on.
- Output rules, because Gmail sanitises signature HTML:
  - Table-based layout with inline styles only. No `<style>`, classes, flexbox, grid, scripts or web fonts.
  - Fonts from a fixed allowlist: Arial, Helvetica, Georgia, Verdana, Tahoma, Trebuchet MS, Times New Roman.
  - Every image has an absolute HTTPS URL, explicit `width`, and `alt`.
  - Empty fields remove their row entirely (no blank lines, no dangling separators).
  - Rendered output must be at most **10,000 characters** (Gmail's signature limit). Exceeding it is a validation error shown in the designer.
- The same renderer serves the public generator, the designer preview and deployment.

### 5.5 Draft and published designs

The signature row holds `draft_design` and `published_design`. The designer edits the draft. Only "Apply" copies draft to published. Automatic deployments always use the published design, so half-finished edits never reach mailboxes.

### 5.6 Deployment and idempotency

- For each employee, render the signature and take its SHA-256. If it equals `employees.applied_hash`, skip the Gmail call (unless the deployment is forced).
- A `deployments` row tracks each run with counters. The UI polls it for progress.
- One Procrastinate task per employee, so a failure for one mailbox never blocks the rest.

### 5.7 Multi-tenancy

Every tenant table has `org_id`. Route handlers get the organisation from a `current_org` dependency derived from the session, never from a URL or form parameter. Every query filters by `org_id`. Add a test that proves one organisation cannot read or change another's rows.

### 5.8 Project layout

```
app/
  main.py            # app factory, middleware, router registration
  config.py          # Settings (pydantic-settings)
  db.py              # engine, session dependency
  models/            # SQLAlchemy models
  web/               # routers: public, auth, onboarding, dashboard, employees,
                     #          designer, banners, analytics, billing, tracking, webhooks
  services/          # sync, deploy, links, assets, billing, plans
  google/            # gateway interfaces, real and fake implementations, fixtures
  storage/           # Storage interface, r2 and local backends
  signatures/        # schema.py, render.py, templates/, social icons list
  tasks/             # Procrastinate app and task definitions
  templates/         # Jinja pages and HTMX partials
  static/            # css, vendor js, social icon PNGs
alembic/
tests/
Dockerfile
docker-compose.yml   # Postgres for local dev
render.yaml
```

Routers stay thin: parse input, call a service, render a template. Business logic lives in `services/`.

## 6. Data model

UUID primary keys unless stated. All timestamps are `timestamptz` in UTC. Every table except `organizations` and `billing_events` has an indexed `org_id` foreign key with `ON DELETE CASCADE`.

### 6.1 Tables

**organizations**: `id`, `domain` (unique), `name`, `google_customer_id`, `status` (`pending` | `connected` | `disconnected`), `owner_user_id` (nullable), `plan` (`free` | `paid`), `tracking_enabled` (default true), `daily_enforce` (default true), `connected_at`, `last_sync_at`, `last_sync_status`, `last_sync_error`, `created_at`.

**users** (admins who sign in): `id`, `org_id`, `email` (unique), `google_sub` (unique), `name`, `picture_url`, `created_at`, `last_login_at`.

**employees**: `id`, `org_id`, `google_user_id`, `email`, `first_name`, `last_name`, `full_name`, `job_title`, `department`, `phone_work`, `phone_mobile`, `photo_url`, `photo_etag`, `status` (`active` | `suspended` | `removed`), `excluded` (bool, admin-controlled), `short_id` (8-char base62, unique, used in tracking URLs), `applied_hash`, `applied_at`, `apply_status` (`never` | `applied` | `failed` | `skipped` | `over_limit`), `apply_error`, `synced_at`. Unique on (`org_id`, `google_user_id`).

**signatures**: `id`, `org_id` (unique in V1), `draft_design` (JSONB), `published_design` (JSONB, nullable), `published_at`, `updated_at`.

**assets**: `id`, `org_id`, `kind` (`logo` | `banner`), `storage_key`, `url`, `width`, `height`, `content_type`, `bytes`, `created_at`.

**banners**: `id`, `org_id`, `asset_id`, `target_url`, `alt_text`, `is_active`, `created_at`, `archived_at`. Partial unique index: one active banner per organisation.

**tracked_links**: `id`, `org_id`, `token` (10-char base62, unique), `kind` (`banner` | `social` | `website`), `ref` (banner id or social network key), `target_url`, `created_at`. Unique on (`org_id`, `kind`, `ref`, `target_url`).

**click_events**: `id` (bigserial), `org_id`, `link_id`, `employee_id` (nullable), `clicked_at`, `is_bot`. Indexes on (`org_id`, `clicked_at`) and (`link_id`, `clicked_at`). No IP address or full user agent is stored.

**deployments**: `id`, `org_id`, `trigger` (`manual` | `test` | `sync` | `enforce` | `plan_change`), `status` (`queued` | `running` | `completed` | `completed_with_errors` | `failed`), `total`, `succeeded`, `failed`, `skipped`, `created_by` (nullable user id), `created_at`, `finished_at`.

**subscriptions**: `id`, `org_id` (unique), `razorpay_subscription_id` (unique), `razorpay_plan_id`, `status`, `quantity`, `current_start`, `current_end`, `cancel_at_cycle_end`, `updated_at`.

**billing_events**: `id`, `razorpay_event_id` (unique), `event_type`, `payload` (JSONB), `received_at`, `processed_at`.

### 6.2 SignatureDesign schema (version 1)

```
schema_version: 1
template: "classic" | "side-logo" | "photo" | "minimal"
brand:
  primary_color: "#RRGGBB"      # names, links, divider
  text_color: "#RRGGBB"
  font_family: one of the allowlist
  font_size: "small" | "medium" | "large"
logo:
  asset_id: uuid | null
  width_px: 60..200
  link_url: url | null
fields:                          # per-employee data toggles
  show_photo, show_job_title, show_department,
  show_phone_work, show_phone_mobile, show_email: bool
company:
  name, tagline, address, phone: str (length-limited)
  website_url: url | null
socials: list of { network, url }, max 6
  network: linkedin | x | facebook | instagram | youtube | github
```

Validation: colours by regex, URLs must be `http` or `https`, all strings length-limited. Unknown keys are rejected. The banner is not part of the design; it comes from the `banners` table.

Templates:
- `classic`: name, title, contact lines stacked, logo underneath.
- `side-logo`: logo on the left, vertical divider, details on the right.
- `photo`: employee photo on the left, details on the right, logo small.
- `minimal`: text only, two or three compact lines.

All four render the socials row, the banner (if active) and the Powered-by line (free plan) in the same order below the main block.

## 7. Conventions

- Type hints everywhere. Async route handlers and services. Sync third-party SDKs (Google, Razorpay, boto3) run via `asyncio.to_thread`.
- All configuration comes from `Settings`. No secrets or URLs hard-coded.
- CSRF: every state-changing request carries a session-bound token. Put it in a meta tag and send it on HTMX requests with `hx-headers`. The Razorpay webhook is exempt and is authenticated by its signature instead.
- Sessions: signed cookie (`SessionMiddleware`), `https_only` in production, `SameSite=Lax`, holding only the user id.
- Escape all user input. Never mark user-provided strings safe in any template.
- Image uploads: PNG, JPEG or GIF only, max 2 MB. Open with Pillow, reject on failure, re-encode (drops metadata), cap dimensions. Reject SVG.
- Tests accompany each phase. No test may call a real external service.
- Log with structured key-value context (`org_id`, `deployment_id`, `employee_id`). Never log signature HTML, tokens or keys.

Established in Phase 0 (follow these in later phases):

- Start every process through `python -m app.cli` (`web`, `worker`, `migrate`, `ping`). It picks an event loop psycopg supports on Windows; plain `uvicorn` does not.
- Database sessions do not commit automatically. The service that writes calls `await session.commit()`, so data is durable before a redirect reaches the browser.
- CSRF is an app-wide dependency (`app/web/csrf.py`). New routes are protected by default; exempt a path only by adding it to `CSRF_EXEMPT_PATHS`.
- `csrf_token()` is a template function. Call it only on pages that submit forms, so public pages set no cookie and stay cacheable.
- Alembic runs synchronously (same psycopg driver, no event loop). Import new model modules in `app/models/__init__.py`. Register new task modules in `import_paths` in `app/tasks/__init__.py`.
- Tests use the `footerpilot_test` database and the `job_queue` fixture (in-memory Procrastinate). Use `127.0.0.1`, not `localhost`, in database URLs.

Established in Phase 1:

- Renderer contract: `render_signature(design, employee, org_ctx)` in `app/signatures/render.py`, with `EmployeeData`, `OrgContext` and `BannerData` defined there. `OrgContext` carries `social_icon_base_url`, `logo_url`, `banner` and `powered_by_url`; the caller resolves assets to URLs. Banner and Powered-by rendering already work. Link rewriting for tracking is not implemented yet (Phase 6 adds a link map to `OrgContext`).
- Decisions about what to show live in `_build_view` in Python. The Jinja templates in `app/signatures/templates/` only lay values out; read the rules at the top of `_macros.html.j2` before editing them.
- Golden files in `tests/signatures/golden/` pin each template's exact HTML. After an intentional template change run `UPDATE_GOLDEN=1 uv run pytest tests/signatures/test_render.py` and review the diff.
- Reuse the validated types in `app/signatures/schema.py` (`text(n)`, `Color`, `HttpUrl`, `OptionalHttpUrl`, `EmailText`) for any form that feeds a signature.
- `preview_document()` wraps a signature for an `<iframe srcdoc>` preview. Elements that HTMX swaps in and JavaScript then styles must not have an `id`: HTMX copies `style` between same-id elements and wipes the script's changes. Use `data-` attributes (see `_generator_preview.html`).
- `/generator/preview` is CSRF-exempt because it is stateless and public. Authenticated routes must never be exempted.

Established in Phase 2:

- Access rules live in `app/web/deps.py`: `require_user`, `require_member` (owner, or anyone from the domain while it has no owner) and `current_org` (connected organisations only). Every `/app` route uses one of them, and the organisation always comes from the session, never from the request.
- Signed-in pages extend `app/templates/app/_layout.html`, which sends the CSRF token with every HTMX request. Pass `user` and `organization` to the template.
- Business logic goes in `app/services/` and returns results as enums or data; user-facing wording stays in the web layer (see `FAILURE_MESSAGES` in `app/web/onboarding.py`).
- Google access goes through `app.state.google` (`get_google` dependency). Add new calls to the interfaces in `app/google/base.py`, then to both `real.py` and `fake.py`. Real calls raise only the gateway's own errors, via `translate_error`.
- The fake gateways decide their behaviour from the email address (table in `README.md`). `/dev/login` exists only when `DEV_LOGIN=true`.
- Test helpers: the `db` fixture (schema built from the Alembic migrations, emptied after each test), `make_user`, and `sign_in(client, user)` from `tests/helpers.py`, which returns headers that pass CSRF. `test_models_match_the_migrated_schema` fails if a model changes without a migration.
- Autogenerated migrations need a manual check: Alembic placed a foreign key before its target table existed (see the `0002` migration).
- Open the app at `APP_BASE_URL` exactly (`127.0.0.1`, not `localhost`), or Google sign-in fails with a state mismatch.

Established in Phase 3:

- Background work: put the logic in `app/services/` taking `db`, `google` and `storage` as arguments, wrap it in a task in `app/tasks/`, and read those three from `get_context()` there. The web layer queues work through a function in the task module (see `request_sync`), never by calling the service directly.
- Give queued jobs `lock=f"org:{id}"` so one organisation's jobs run one at a time, and a `queueing_lock` so repeated requests do not pile up.
- `sync_org` returns `SyncResult.changed_ids`: the employees whose signature-relevant data changed. Phase 5 deploys to exactly those.
- Long-running state shown in the UI uses the pattern in `_sync_status.html`: a fragment that polls itself while work is in progress, stops when it finishes, and fires an `HX-Trigger` event that other parts of the page listen for. `sync_requested_at` makes a job that never ran show as "did not finish" instead of spinning forever.
- Employees are never deleted, only marked `removed`. Queries that list people must filter on `org_id` and usually on status (see `app/services/employees.py`).
- Images always go through `app/services/images.py` (decode, re-encode, strip metadata) and are stored under a key containing a content hash, so a public URL never changes what it shows.
- Tests: `make_owner` gives a connected organisation, `run_jobs` executes queued jobs in-process, and `tests/fakes.py` has a scriptable directory and in-memory storage. The fake Google directory (`app/google/fixtures/directory.json`) is the signed-in admin plus eleven people covering the awkward cases.
- Tables that can hold long values use `table-fixed` with column widths and `truncate`, so one long name cannot push columns out of view.
- Additions to the data model in section 6.1: `employees.has_mailbox` and `organizations.sync_requested_at`.

Established in Phase 4:

- Rendering a signature for a person always goes through `build_org_context` and `employee_data` in `app/services/signatures.py`. Deployment (Phase 5) must use the same two functions, so the preview and the mailbox cannot drift apart.
- Publishing means copying `draft_design` to `published_design` and setting `published_at`. Phase 5 does this when the admin applies the signature; `has_unpublished_changes` and "Discard changes" already work against it.
- The designer form (`app/templates/app/designer.html`) is the pattern for autosaving forms: one `<form>` wraps every control, saves on `input` with a 300 ms delay, and the response swaps the preview plus out-of-band error slots (`_designer_errors.html`) and status (`_designer_status.html`). On a validation error the response sets `HX-Reswap: none`, so nothing is saved and the last good preview stays.
- The draft changes only through `POST /app/designer/draft`. An upload just stores the file and fires `designer-changed`; the form then saves itself with the new asset id. Asset ids from the form are always checked against the organisation (`get_logo`).
- HTMX details that matter inside that form: exclude file inputs from the form trigger (`input[target.type!='file']`), give buttons with their own request `hx-sync="this:replace"` and `hx-params="none"`, and remember that `hx-delete` sends parameters in the URL.
- `app/static/js/preview.js` keeps any `<iframe data-signature-frame>` fitted to its content; load it on every page that previews a signature.
- Uploaded images go through `normalise_logo` (PNG, JPEG or GIF, at most 2 MB, re-encoded, at most 400 px). Banners in Phase 6 should follow the same shape with their own size limit.
- Storage keys are validated as text (`LocalStorage._path`), never by resolving paths on disk: resolving while other uploads create the same folders is unstable on Windows.
- When checking the app by hand or by script, never start a second worker against the development database: workers share one job queue, so a fake-mode worker could process a real organisation's jobs. Use a separate database (`DATABASE_URL`), port and `MEDIA_ROOT`.

Established in Phase 5:

- Deployment lives in `app/services/deploy.py` (logic) and `app/tasks/deploy.py` (jobs). Start one with `start_deployment(session, organization, trigger, ...)`; it creates the row and queues one `apply_signature` job per eligible employee, locked per mailbox so different mailboxes run in parallel.
- `_eligible()` in `services/deploy.py` is the single definition of who gets the signature (active and not excluded). Phase 7 adds the plan limit there and nowhere else.
- Results are recorded through `_record`, which updates the employee and the deployment counters in one transaction and closes the deployment when the last mailbox reports. A mailbox that already has the right signature counts as succeeded without a call to Google. Skips (no mailbox) are not errors: only failures make a run `completed_with_errors`.
- Gmail write errors are mapped in `translate_write_error`: `RetryableError` (retried by the queue, 5 tries, waits of 3, 9, 27, 81 s), `SkipError`, `PermanentError`, and `NotAuthorizedError`, which disconnects the organisation and stops the deployment.
- The designer's apply endpoints go through `_save_form` first, so what is applied is exactly what is on screen. "Apply to me only" runs in the request (no queue) and publishes nothing. "Apply to everyone" publishes, then starts a forced manual deployment, so it also overwrites signatures employees edited themselves.
- One manual deployment per organisation at a time is guaranteed by a partial unique index, not by application checks. Deployments still unfinished after 30 minutes are marked failed (`expire_stale_deployments`) so they cannot block new ones.
- The deployment page lists the *current* failed and skipped employees (latest result per person), not a per-run history. There is no per-run item table.
- Scheduled jobs are in `app/tasks/periodic.py`: hourly `sync_all`, daily `enforce_all` at 03:30 UTC. After every successful sync, `deploy_changes_after_sync` updates the people whose details changed, if a design is published.
- The dashboard uses `require_set_up_org` so a disconnected organisation sees an explanation and a Reconnect button; every other page still sends it to the wizard.
- `Settings.images_are_public` is false with local storage or a loopback address; the designer warns that images will not load in real email.
- Tests: use the `mail` fixture pattern (a `ScriptedGmail` from `tests/fakes.py`) for anything that deploys. The app's default fake Gmail deliberately has one mailbox that always fails and one that fails once, which suits manual checks but leaves an in-process test deployment unfinished.

## 8. Phase 0: Foundations

**Status: complete (2026-11-19).** Commands and setup are in `README.md`.

**Goal:** an empty but runnable, deployable application.

Tasks:
1. Initialise the uv project, ruff and pytest config, `.gitignore`, `.env.example` listing every setting in section 16.
2. `config.py` with `Settings`, including the production guard (fail at startup if `ENV=production` and `GOOGLE_MODE=fake`, `DEV_LOGIN=true` or `STORAGE_BACKEND=local`).
3. Async SQLAlchemy engine and session dependency. Alembic configured for async, first empty migration.
4. Procrastinate app in `app/tasks/`, a trivial test task, and its schema applied as part of the migration step.
5. App factory with session middleware, CSRF helper, static files, error pages (404, 500), `/healthz` (checks the database).
6. Base Jinja layout with Tailwind (standalone CLI build step), vendored HTMX and Alpine.
7. `docker-compose.yml` with Postgres. `Dockerfile` (multi-stage, builds Tailwind CSS, non-root user). Commands documented in `README.md`: run web, run worker, migrate, test.

Done when:
- `docker compose up` plus the documented commands serve a styled placeholder home page and `/healthz` returns 200.
- The worker starts and executes the test task.
- `pytest` and `ruff check` pass.

## 9. Phase 1: Signature engine and public generator

**Status: complete (2026-11-19)**, except one manual check left for the owner: paste a generated signature into a real Gmail signature box and compare it with the preview. This needs `ASSET_BASE_URL` to be a public URL, otherwise the social icons will not load in Gmail.

**Goal:** the renderer and templates exist, and anyone can make a personal signature without signing up (idea.md 5.1). No Google or database dependency.

Tasks:
1. `SignatureDesign` model and validation (section 6.2).
2. The four templates and `render_signature` (section 5.4), including the 10,000-character check. Social icon PNGs (one neutral set, 2x resolution) in `app/static/social/`, referenced by absolute URL from `ASSET_BASE_URL`.
3. Public pages: `/` (landing), `/email-signature-generator`, with proper titles, meta descriptions, `sitemap.xml` and `robots.txt`.
4. Generator UI: form on the left (name, title, company, phone, email, website, logo image URL, socials, template, colours, font), live preview on the right. The form posts to `/generator/preview` via HTMX (debounced about 300 ms) and the response is shown in an `<iframe srcdoc>` so page CSS cannot leak into the preview.
5. "Copy signature" (rich HTML to the clipboard via the Clipboard API, `text/html` plus a plain-text fallback) and "Copy HTML source". Short paste instructions for Gmail, Outlook and Apple Mail.
6. A call to action: "Need this for your whole team?" linking to sign-up.
7. Simple per-IP rate limit on `/generator/preview`.

The generator accepts image URLs only. No anonymous uploads, nothing is stored, no tracking links are added.

Tests: golden-file snapshots for each template with full data, minimal data and the long-name case; empty fields leave no blank rows; HTML-special characters in every field are escaped; output over the limit raises the validation error; invalid colours and URL schemes are rejected.

Done when:
- A signature built in the generator pastes into Gmail's signature box and looks like the preview.
- All four templates render cleanly with missing title, missing phone and missing logo.

## 10. Phase 2: Sign-in and Google connection wizard

**Status: complete (2026-11-19).** Verified against real Google with the footerpilot.com Workspace (Business Base plan, India): Google sign-in works and verification connects the domain once delegation is added.

**Goal:** an admin signs in and connects their Workspace domain (idea.md 5.2).

Tasks:
1. Migrations for `organizations` and `users`.
2. Google sign-in with Authlib (`openid email profile`). Read the `hd` claim: if absent (personal Gmail), show a page explaining a Google Workspace account is required. Find or create the organisation by domain and the user by `google_sub`.
3. Ownership rules:
   - An organisation is `pending` and has no owner until verification succeeds.
   - The user who completes verification becomes the owner.
   - Once an owner exists, any other user from that domain sees "Your company is already set up, contact <owner email>" and gets no access.
4. `require_user` and `require_connected_org` dependencies. Unconnected organisations are redirected to the wizard.
5. Wizard at `/app/setup`:
   - Step 1: what FooterPilot can and cannot access, in plain language.
   - Step 2: instructions to add the Client ID and scopes in the Admin console, with a copy button for the Client ID, a copy button for the comma-separated scopes, a button opening the Admin console page, screenshot placeholders, a video placeholder and the "Book a setup call" link.
   - Step 3: "Verify connection" button.
6. Verification service: mint a delegated token for each scope as the admin, then call Directory `users.get` on the admin's own address and require `isAdmin` to be true. Store `google_customer_id`, set status `connected`, set the owner. Map failures to specific messages:
   - `unauthorized_client`: not authorised yet or scopes do not match; mention that changes can take a few minutes to apply.
   - 403 or `isAdmin` false: the signed-in user is not a Workspace admin.
   - Anything else: generic retry message, error logged.
7. Real and fake gateway implementations for `verify_admin` and `verify_access`. `/dev/login` for fake mode.
8. Sign-out, and a minimal `/app` dashboard shell with navigation.

Tests: `hd` handling; ownership rules including the second-user case; each verification failure maps to the right message; routes under `/app` reject anonymous users; tenant isolation test scaffold.

Done when:
- In fake mode, `/dev/login` then the wizard reaches the dashboard.
- In real mode on the test domain, verification fails with the right message before DWD is added and succeeds after.

## 11. Phase 3: Employee sync

**Status: built (2026-11-19).** Verified in fake mode end to end, and against the real footerpilot.com directory with local storage, including a title change picked up by Sync now and a real profile photo. Not yet verified against real services: upload to R2 (blocked on the development network by TLS inspection; see the README note).

Lesson from real-mode testing: Google's Directory API omits `thumbnailPhotoEtag` unless `thumbnailPhotoUrl` is in the same field mask. The fake and the unit tests could not catch that, so any new Google field must be checked once against the live API.

**Goal:** employee details are pulled from the directory and kept in the database (idea.md 5.3).

Tasks:
1. Migration for `employees`.
2. `DirectoryGateway.list_users`: page through `users.list` (`customer="my_customer"`, full projection, 500 per page). Map: primary email, given, family and full name, primary organisation title and department, work and mobile phones, suspended and archived flags, mailbox-setup flag, photo etag.
3. `sync_org(org_id)` service:
   - Upsert by `google_user_id`. New users get a `short_id`.
   - Suspended or archived users become `suspended`. Users no longer returned become `removed`. Neither is deleted.
   - Photos: when the etag is new or changed, fetch via `users.photos.get`, decode, normalise with Pillow to a square JPEG (about 160 px), upload to storage at a key that includes a content hash, and store the public URL. No photo means `photo_url` is null.
   - Record `last_sync_at`, status and error on the organisation. A delegation failure (`unauthorized_client`) sets the organisation to `disconnected`.
   - Return the set of employees whose data changed (used in Phase 5).
4. Procrastinate task wrapping `sync_org`, with a queueing lock per organisation so syncs never stack up.
5. Trigger the first sync automatically after verification, with a wizard step 4 that polls for completion and shows "N employees found".
6. `/app/employees`: table with photo, name, title, email, status, signature status, an include/exclude toggle, search by name or email, and a "Sync now" button with last-synced time.
7. Storage backends (`r2`, `local`).

Tests (fake directory): first sync creates rows; a title change updates the row and is reported as changed; a missing user becomes `removed`; a suspended user becomes `suspended`; an unchanged photo etag triggers no download; pagination across multiple pages.

Done when:
- The employees page shows the directory of the test domain with photos.
- Editing a title in the Google Admin console and pressing "Sync now" updates the row.

## 12. Phase 4: Designer and live preview

**Status: complete (2026-11-19).** Verified in a browser in fake mode: preview updates in under a second, "Preview as" shows each employee's data, logo upload and removal work. Additions beyond the task list: placeholder logo and photo on the template picker (so the layouts look different before a logo exists), and the free plan's "Powered by" line already shows in the preview.

**Goal:** the admin designs the company signature and previews it for any employee (idea.md 5.4, 5.5).

Tasks:
1. Migrations for `signatures` and `assets`.
2. Template picker shown when no draft exists (wizard step 5): four cards with sample renders. Choosing one creates the draft with sensible defaults (company name from the organisation, domain as website).
3. Designer at `/app/designer`: two columns.
   - Left: grouped form (Template, Brand, Logo, Employee fields, Company details, Social links).
   - Right: preview iframe, "Preview as" employee selector with previous/next, and two built-in edge cases: "Longest name and title" and "Missing optional fields".
4. `POST /app/designer/draft` (HTMX, debounced): validate, save the draft, return the preview fragment. On a validation error, return inline field errors and leave the saved draft unchanged.
5. Logo upload endpoint following the image rules in section 7. Resize to at most 400 px wide (2x of the 200 px display maximum), store, create the asset row.
6. A character-count indicator showing rendered length against the 10,000 limit for the previewed employee, with a warning from 9,000.
7. "Discard changes" resets the draft to the published design when one exists.
8. An "Unpublished changes" badge when draft and published differ.

The preview renders with the same `org_ctx` builder that deployment will use (stub the link map and banner until Phase 6).

Tests: draft save and reload round-trip; invalid input never overwrites the draft; logo upload rejects oversized files, non-images and SVG; preview for an employee from another organisation is refused.

Done when:
- Changing any form control updates the preview in under a second without a page reload.
- Switching "Preview as" shows real data for different employees.

## 13. Phase 5: Apply and automatic updates

**Status: built (2026-11-20).** Verified in a browser in fake mode: apply to me, apply to everyone with live progress, automatic retry of a temporary error, failure list and Retry failed, discard, settings. Not yet done: the manual checks on the real test domain listed below (they write to real mailboxes, so the owner runs them); record the results in `README.md`. Deviations from the task list: "Apply to me only" runs immediately instead of through the queue; "Apply to everyone" is always forced; the failure list shows each person's latest result instead of a per-run history.

**Goal:** one click puts the signature into every mailbox, and it stays correct without further action (idea.md 5.6, 5.7).

Tasks:
1. Migration for `deployments`.
2. `GmailGateway.set_signature` (real and fake). Error mapping in the real gateway:
   - 429 and 5xx: `RetryableError`.
   - 400 `failedPrecondition` (mail service not enabled) and 404: `SkipError` with a reason.
   - `unauthorized_client` at token minting: `ConnectionLostError`.
   - Anything else: `PermanentError`.
3. Deployment service:
   - `start_deployment(org, trigger, employee_ids=None, force=False)` selects eligible employees (`active`, not `excluded`, within plan limit, see Phase 7), creates the row and enqueues one `apply_signature` task per employee. Only one `manual` deployment may run per organisation at a time.
   - `apply_signature(employee_id, deployment_id, force)`: build `org_ctx`, render with the published design, compare hashes, call the gateway if needed, update the employee's `applied_*` fields and the deployment counters in one atomic `UPDATE`. When processed equals total, set the final status.
   - Retries: exponential backoff, 5 attempts, on `RetryableError` only. On the final failed attempt record the failure on the employee and the counter instead of raising. `ConnectionLostError` marks the organisation `disconnected` and fails the deployment.
4. Designer actions:
   - "Apply to me only": publishes nothing; renders the current draft for the admin's own employee record and writes it to the admin's mailbox (`trigger=test`).
   - "Apply to everyone": confirmation dialog stating the number of mailboxes, then copy draft to published and start a `manual` deployment.
5. Progress UI: a panel polling `/app/deployments/{id}` every 2 seconds showing succeeded, failed and skipped, then a result summary with a list of failed or skipped employees and reasons, and "Retry failed".
6. Automatic updates (only for organisations that are `connected` and have a published design):
   - Hourly periodic task enqueues `sync_org` for every connected organisation. After each sync, start a `sync` deployment for new and changed employees.
   - Daily periodic task starts a forced `enforce` deployment for organisations with `daily_enforce` on, which overwrites signatures employees edited themselves. Expose this as a toggle in `/app/settings`.
7. Dashboard: connection status, employee counts, last sync, last deployment result, and a clear notice with a "Reconnect" link to the wizard when the organisation is `disconnected`.

Tests (fake gateways): full deployment reaches `completed`; unchanged hash makes no gateway call; a forced run calls for everyone; retryable errors retry then succeed; a skip is recorded with its reason; connection loss marks the organisation; a new employee appearing in a sync gets a signature; a second concurrent manual deployment is refused; "Apply to me only" touches exactly one mailbox and leaves `published_design` unchanged.

Manual checks on the real test domain (record results in the README):
- A user with no existing signature, one with an existing signature, and one with several signatures saved in Gmail: confirm the new signature shows and is used for new mail.
- The Gmail mobile app shows the signature.
- Images, links and layout survive in mail received by Gmail, Outlook and Apple Mail.

Done when:
- "Apply to everyone" updates every included mailbox in the test domain and the summary matches reality.
- Adding a user in the Admin console results in a signature for them after the next sync with no admin action.
- Changing a title in the Admin console changes that employee's signature after the next sync.

## 14. Phase 6: Banners and click tracking

**Goal:** a promotional banner in every signature and click counts for the admin (idea.md 5.8, 5.9).

Tasks:
1. Migrations for `banners`, `tracked_links`, `click_events`.
2. `/app/banner`: upload an image (resized to at most 1200 px wide, displayed at up to 600 px), target URL, alt text, live preview inside the signature. "Activate" archives the previous banner, publishes this one and starts a deployment after the same confirmation dialog. "Remove banner" does the same with no banner. Past banners are listed with their click totals.
3. Link service: `build_link_map(org)` gets or creates `tracked_links` rows for the active banner, the website and each social link, and returns target URL to tracking URL. It runs when a design is published and when the banner changes, and feeds `org_ctx`. If `tracking_enabled` is off the map is empty and original URLs are used.
4. The renderer rewrites those links to `{TRACKING_BASE_URL}/c/{token}?e={employee.short_id}`. `mailto:` and `tel:` links are never tracked.
5. `GET /c/{token}`: look up the link, record a `click_events` row, respond with a 302 to the stored `target_url`. Unknown tokens return 404. `HEAD` requests redirect without recording. Mark `is_bot` when the user agent matches a short list of known scanners and crawlers. The redirect target only ever comes from the database, never from the request.
6. `/app/analytics`: range selector (7, 30, 90 days), total clicks, a per-day bar chart drawn with plain HTML and CSS, and a table of links (banner, website, each social) with counts. Bot clicks are excluded. Include a short note that mail security scanners can inflate counts.
7. Tracking toggle in `/app/settings` (changing it starts a deployment).

Tests: click records and redirects; unknown token is 404; bot and `HEAD` handling; a banner swap produces a new link and keeps old counts; counts are scoped to the organisation; tracking off renders original URLs.

Done when:
- Activating a banner puts it in every signature, clicking it in a received mail lands on the target, and the click appears on the analytics page.

## 15. Phase 7: Plans and billing (Razorpay)

**Goal:** a free tier with a Powered-by link and a paid per-user plan with self-serve checkout (idea.md 5.10).

Plan rules (`services/plans.py`, the single source of truth):
- **Free**: up to `FREE_PLAN_MAX_EMPLOYEES` included employees. Signatures carry "Powered by FooterPilot" linking to the marketing site. If more employees are included than the limit, deployments are blocked and the dashboard asks the admin to upgrade or exclude employees. Employees who would exceed the limit after a sync are set to `over_limit` and not deployed.
- **Paid**: no employee limit, no Powered-by link. Billed quantity is the number of included active employees.
- `organizations.plan` is derived from the subscription status: `authenticated`, `active` and `pending` (payment being retried) mean paid; `halted`, `cancelled`, `completed` and `expired` mean free.

Tasks:
1. Migrations for `subscriptions` and `billing_events`.
2. Public `/pricing` page and `/app/billing` page (current plan, seat count, next charge date, upgrade or cancel).
3. Upgrade flow:
   - Server creates a Razorpay subscription (`plan_id` from config, `quantity` = included active employees, minimum 1, `total_count` per Razorpay's rules, `notes.org_id`).
   - The page opens Razorpay Checkout with the `subscription_id`.
   - The success handler posts `razorpay_payment_id`, `razorpay_subscription_id` and `razorpay_signature` to the server, which verifies the signature (HMAC-SHA256 of `payment_id|subscription_id` with the key secret; use the SDK helper) and shows "Payment received, activating".
   - Plan changes are applied **only** from webhooks.
4. `POST /webhooks/razorpay`: verify `X-Razorpay-Signature` against the raw request body, store the event (unique event id makes replays harmless), then update the subscription row and the organisation's plan. Handle the `subscription.*` events: authenticated, activated, charged, pending, halted, cancelled, completed, paused, resumed, updated.
5. Any plan change starts a `plan_change` deployment so the Powered-by link is added or removed.
6. Seat sync: a daily task compares included active employees with the subscription quantity and, if different, updates the Razorpay subscription with the change scheduled for the end of the cycle. New hires therefore get signatures immediately and are billed from the next cycle.
7. Cancel: cancel at cycle end through the API; the organisation stays paid until Razorpay reports the end.
8. Enforce the free-plan rules in `start_deployment` and in the sync flow.

Open points to confirm against Razorpay docs and the account before coding (do not guess):
- Whether quantity updates are allowed for the payment methods enabled on the account (updates have been limited to card subscriptions). If not, fall back to a seat count the admin sets manually on the billing page.
- The maximum `total_count` for a monthly plan.
- Currency and international card support for subscriptions on this account.

Tests: webhook signature accepted and rejected; replayed event processed once; each status maps to the right plan; upgrade removes and downgrade restores the Powered-by link in rendered output; free limit blocks deployment and marks extra employees `over_limit`; seat sync issues an update only when the count differs. Razorpay is always mocked.

Done when:
- In Razorpay test mode, an organisation upgrades through Checkout, the webhook flips it to paid, and signatures are redeployed without the Powered-by link.
- Cancelling returns it to free at cycle end and the free-plan rules apply.

## 16. Phase 8: Trust pages, hardening and launch

**Goal:** safe to put in front of real customers (idea.md 5.11).

Tasks:
1. Public pages: `/privacy`, `/terms`, and `/security` ("What we can and cannot access": the two scopes in plain language, no mail access, what data is stored, where images are hosted, how to revoke access in the Admin console). Legal text is placeholder copy clearly marked for review by the owner.
2. `/app/settings`: organisation name, tracking toggle, daily enforce toggle, "Disconnect" (stops sync and deployment, leaves existing signatures in place) and "Delete account" (typed confirmation, cancels any subscription, deletes all rows and stored images).
3. Security pass: security headers (HSTS, `X-Content-Type-Options`, frame options, a Content-Security-Policy that allows Razorpay Checkout and the preview iframes), cookie flags, rate limits on sign-in, preview and `/c/`, dependency audit, confirm no secrets or signature HTML in logs.
4. Sentry wiring and a worker health signal (log a heartbeat from a periodic task).
5. `render.yaml`: web service, worker service, Postgres, a pre-deploy step that runs Alembic and the Procrastinate schema command. Production environment variables documented.
6. Empty states, loading states and error messages reviewed across every page. Mobile-friendly public pages.
7. End-to-end run on the real test domain following a written checklist: sign up, connect, sync, design, test apply, apply to all, banner, click, upgrade, new hire, cancel, delete. Target: under 15 minutes from sign-up to signatures applied.

Done when:
- The production deployment passes the end-to-end checklist.
- All earlier "Done when" lists still pass.

Settings to define in `config.py` and `.env.example`:

| Setting | Purpose |
|---|---|
| `ENV` | `dev` or `production` |
| `APP_BASE_URL`, `MARKETING_BASE_URL`, `TRACKING_BASE_URL`, `ASSET_BASE_URL` | Public URLs (may all be one host in V1) |
| `SECRET_KEY` | Session signing |
| `DATABASE_URL` | Postgres |
| `GOOGLE_MODE`, `DEV_LOGIN` | Fake mode switches |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | Admin sign-in |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Base64 service-account key |
| `GOOGLE_DWD_CLIENT_ID` | Shown in the wizard |
| `STORAGE_BACKEND`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL` | Image storage |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_PLAN_ID` | Billing |
| `FREE_PLAN_MAX_EMPLOYEES` | Default 5 |
| `PRICE_DISPLAY` | Text shown on the pricing page |
| `SETUP_CALL_URL`, `SUPPORT_EMAIL` | Wizard and footer links |
| `SENTRY_DSN` | Optional |

## 17. Owner prerequisites

The human owner provides these. Phases 0 to 1 need none of them; fake mode covers development of the rest.

| Needed by | Item |
|---|---|
| Phase 2 | Google Cloud project with Admin SDK API and Gmail API enabled, a service account and key, and an OAuth web client for sign-in (consent screen published) |
| Phase 2 | A Google Workspace test domain with super-admin access and at least 3 users |
| Phase 3 | Cloudflare R2 bucket with a public custom domain and API keys |
| Phase 7 | Razorpay account with Subscriptions enabled, a monthly per-unit plan, API keys and a webhook secret |
| Phase 8 | Render account, production domain name, final legal text, price and free-tier limit |

## 18. Known risks

- **Domain-wide delegation friction.** Pasting a Client ID and scopes into the Admin console is the main drop-off point. The wizard copy, screenshots and error messages matter more than any other UI in V1.
- **Google review requirements.** The DWD route is used because the admin grants access directly in the Admin console. Before onboarding outside customers, confirm in Google's current policies that no additional app verification applies to this model.
- **Employees can still edit their signature in Gmail.** Google offers no lock. The daily enforce run is the mitigation.
- **Gmail's 10,000-character limit and HTML sanitising.** Keep templates lean and re-test rendering whenever a template changes.
- **Click counts are approximate.** Mail security scanners follow links. V1 filters only obvious bots.
- **Razorpay constraints** on quantity updates and international payments (see Phase 7 open points).
