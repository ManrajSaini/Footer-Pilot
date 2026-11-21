"""Counted links: rewriting them in signatures, the /c/<token> address, and analytics."""

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.google.base import GoogleGateways
from app.main import SESSION_COOKIE
from app.models import (
    ClickEvent,
    Deployment,
    DeploymentTrigger,
    Employee,
    Organization,
    TrackedLink,
)
from app.services import analytics as analytics_service
from app.services.analytics import axis_ticks
from app.services.links import is_bot, new_token
from app.services.sync import sync_org
from tests.fakes import ScriptedGmail
from tests.helpers import HTMX, form, sign_in

APPLY = "/app/designer/apply"
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0 Safari/537.36"}
# A counted link as it appears in a signature: (token, employee short id).
TRACKED = re.compile(r'href="http://testserver/c/([A-Za-z0-9]{10})\?e=([A-Za-z0-9_-]+)"')


@pytest.fixture
def mail(app: FastAPI) -> ScriptedGmail:
    gmail = ScriptedGmail()
    app.state.google = GoogleGateways(directory=app.state.google.directory, gmail=gmail)
    return gmail


@pytest.fixture
async def owner(app: FastAPI, db: Database, make_owner, mail):
    user = await make_owner("admin@acme.test")
    assert (await sync_org(db, app.state.google, app.state.storage, user.org_id)).ok
    return user


@pytest.fixture
async def designing(client: AsyncClient, owner):
    """Signed in, with the Classic template chosen. Returns HTMX request headers."""
    headers = sign_in(client, owner)
    await client.post("/app/designer/start", data={"template": "classic"}, headers=headers)
    return {**headers, **HTMX}


@pytest.fixture
async def live(client: AsyncClient, designing, run_jobs, mail):
    """The default design (website https://acme.test) applied to every mailbox."""
    response = await client.post(APPLY, data=form(), headers=designing)
    assert response.status_code == 200, response.text
    await run_jobs()
    return designing


async def rows(db: Database, model) -> list:
    async with db.session() as session:
        return list((await session.execute(select(model))).scalars())


async def employee(db: Database, email: str) -> Employee:
    async with db.session() as session:
        return (await session.execute(select(Employee).where(Employee.email == email))).scalar_one()


async def add_clicks(db: Database, link: TrackedLink, when: datetime, count: int = 1, bot=False):
    async with db.session() as session:
        for _ in range(count):
            session.add(
                ClickEvent(org_id=link.org_id, link_id=link.id, clicked_at=when, is_bot=bot)
            )
        await session.commit()


# --- Recognising automated clients ----------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (BROWSER["User-Agent"], False),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148 Safari/604.1",
            False,
        ),
        ("Mozilla/5.0 (Macintosh) Gecko/20100101 Thunderbird/128.0", False),
        (None, True),
        ("", True),
        ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", True),
        ("Mozilla/5.0 (Windows NT 10.0) GoogleImageProxy", True),
        ("Barracuda Sentinel (EE)", True),
        ("Mimecast-URL-Protection", True),
        ("curl/8.9.1", True),
        ("python-requests/2.32", True),
        ("Mozilla/5.0 HeadlessChrome/140.0", True),
        ("facebookexternalhit/1.1", True),
    ],
)
def test_is_bot(agent, expected):
    assert is_bot(agent) is expected


def test_tokens_are_ten_url_safe_characters():
    tokens = {new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(re.fullmatch(r"[A-Za-z0-9]{10}", token) for token in tokens)


# --- Links in the signature -----------------------------------------------------------


async def test_applied_signature_carries_counted_links(db, owner, mail, live):
    admin = await employee(db, "admin@acme.test")
    html = mail.signature("admin@acme.test")

    (link,) = await rows(db, TrackedLink)
    assert (link.kind, link.ref, link.target_url) == ("website", "", "https://acme.test")
    assert link.org_id == owner.org_id
    # The website is linked through the counting address, with this person's short id.
    assert set(TRACKED.findall(html)) == {(link.token, admin.short_id)}
    assert 'href="https://acme.test"' not in html
    # The address is still shown as text, and email links are never counted.
    assert "acme.test" in html
    assert 'href="mailto:admin@acme.test"' in html


async def test_each_person_gets_their_own_short_id(db, mail, live):
    found = {}
    for email in ("admin@acme.test", "chen.wei@acme.test"):
        ((token, short_id),) = set(TRACKED.findall(mail.signature(email)))
        found[email] = (token, short_id)
        assert short_id == (await employee(db, email)).short_id
    tokens = {token for token, _ in found.values()}
    assert len(tokens) == 1  # one link for the whole company
    assert len({short_id for _, short_id in found.values()}) == 2


async def test_social_links_are_counted_too(client, db, mail, designing, run_jobs):
    data = form(social_linkedin="https://www.linkedin.com/company/acme", company_phone="555 0100")
    await client.post(APPLY, data=data, headers=designing)
    await run_jobs()

    links = {(link.kind, link.ref): link for link in await rows(db, TrackedLink)}
    assert set(links) == {("website", ""), ("social", "linkedin")}
    html = mail.signature("admin@acme.test")
    assert {token for token, _ in TRACKED.findall(html)} == {link.token for link in links.values()}
    assert "linkedin.com" not in html.replace("linkedin.png", "")
    assert "/c/" not in "".join(re.findall(r'href="tel:[^"]*"', html))


async def test_previewing_a_draft_creates_no_links(client, db, designing):
    response = await client.post(
        "/app/designer/draft",
        data=form(social_x="https://x.com/acme"),
        headers=designing,
    )
    assert response.status_code == 200
    assert await rows(db, TrackedLink) == []
    # With no link row yet, the preview shows the real addresses.
    assert "https://x.com/acme" in response.text and "/c/" not in response.text


async def test_applying_again_keeps_the_same_link(client, db, mail, live, run_jobs):
    (before,) = await rows(db, TrackedLink)
    await client.post(APPLY, data=form(company_tagline="Hello"), headers=live)
    await run_jobs()
    (after,) = await rows(db, TrackedLink)
    assert after.token == before.token


async def test_a_changed_address_gets_a_new_link_and_the_old_one_still_works(
    client, db, mail, live, run_jobs
):
    (old,) = await rows(db, TrackedLink)
    await client.post(APPLY, data=form(company_website_url="https://acme.example"), headers=live)
    await run_jobs()

    links = {link.target_url: link for link in await rows(db, TrackedLink)}
    assert set(links) == {"https://acme.test", "https://acme.example"}
    new = links["https://acme.example"]
    assert new.token != old.token
    assert {token for token, _ in TRACKED.findall(mail.signature("admin@acme.test"))} == {new.token}
    # Emails sent before the change still lead where they did.
    response = await client.get(f"/c/{old.token}", headers=BROWSER)
    assert response.headers["location"] == "https://acme.test"


# --- The counting address -------------------------------------------------------------


async def test_click_is_recorded_and_redirects(client, db, owner, live):
    (link,) = await rows(db, TrackedLink)
    admin = await employee(db, "admin@acme.test")
    client.cookies.clear()  # a recipient of the email: not signed in

    response = await client.get(f"/c/{link.token}?e={admin.short_id}", headers=BROWSER)
    assert response.status_code == 302
    assert response.headers["location"] == "https://acme.test"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in response.headers
    assert SESSION_COOKIE not in client.cookies

    (event,) = await rows(db, ClickEvent)
    assert (event.org_id, event.link_id, event.employee_id) == (owner.org_id, link.id, admin.id)
    assert event.is_bot is False
    assert abs((datetime.now(UTC) - event.clicked_at).total_seconds()) < 60


@pytest.mark.parametrize(
    "token", ["AAAAAAAAAA", "short", "x" * 11, "abcde-ghij", "..%2f..%2fapp", "%00%00%00%00%00"]
)
async def test_unknown_or_malformed_token_is_not_found(client, db, live, token):
    response = await client.get(f"/c/{token}", headers=BROWSER)
    assert response.status_code == 404
    assert await rows(db, ClickEvent) == []


async def test_head_redirects_without_recording(client, db, live):
    (link,) = await rows(db, TrackedLink)
    response = await client.head(f"/c/{link.token}", headers=BROWSER)
    assert response.status_code == 302
    assert response.headers["location"] == "https://acme.test"
    assert await rows(db, ClickEvent) == []
    assert (await client.head("/c/AAAAAAAAAA")).status_code == 404


async def test_scanner_clicks_are_stored_but_not_counted(client, db, owner, live):
    (link,) = await rows(db, TrackedLink)
    for agent in ("Mimecast-URL-Protection", "curl/8.9.1"):
        response = await client.get(f"/c/{link.token}", headers={"User-Agent": agent})
        assert response.status_code == 302  # a scanner is still sent on
    await client.get(f"/c/{link.token}", headers=BROWSER)

    events = await rows(db, ClickEvent)
    assert sorted(event.is_bot for event in events) == [False, True, True]
    async with db.session() as session:
        stats = await analytics_service.load(session, owner.org_id, 7)
    assert stats.total == 1


async def test_destination_never_comes_from_the_request(client, db, live):
    (link,) = await rows(db, TrackedLink)
    response = await client.get(
        f"/c/{link.token}?url=https://evil.example&next=//evil.example&to=https://evil.example",
        headers={**BROWSER, "Referer": "https://evil.example"},
    )
    assert response.headers["location"] == "https://acme.test"


@pytest.mark.parametrize("short_id", ["", "nobody00", "x" * 40, "a b", "<script>"])
async def test_click_with_an_unknown_employee_is_counted_without_one(client, db, live, short_id):
    (link,) = await rows(db, TrackedLink)
    response = await client.get(f"/c/{link.token}", params={"e": short_id}, headers=BROWSER)
    assert response.status_code == 302
    (event,) = await rows(db, ClickEvent)
    assert event.employee_id is None


async def test_click_cannot_be_credited_to_another_organisations_employee(
    app, client, db, live, make_owner
):
    globex = await make_owner("admin@globex.test")
    assert (await sync_org(db, app.state.google, app.state.storage, globex.org_id)).ok
    outsider = await employee(db, "admin@globex.test")
    (link,) = await rows(db, TrackedLink)

    await client.get(f"/c/{link.token}?e={outsider.short_id}", headers=BROWSER)
    (event,) = await rows(db, ClickEvent)
    assert event.org_id == link.org_id
    assert event.employee_id is None


async def test_click_address_needs_no_csrf_token_or_session(client, db, live):
    (link,) = await rows(db, TrackedLink)
    client.cookies.clear()
    assert (await client.get(f"/c/{link.token}", headers=BROWSER)).status_code == 302
    # Only GET and HEAD exist.
    assert (await client.post(f"/c/{link.token}")).status_code in (403, 405)


# --- Switching click counting off and on ----------------------------------------------


async def test_tracking_off_puts_the_real_addresses_back(client, db, owner, mail, live, run_jobs):
    (link,) = await rows(db, TrackedLink)
    page = (await client.get("/app/settings")).text
    assert re.search(r'name="tracking_enabled"[^>]*checked', page)  # on by default

    off = await client.post("/app/settings", data={"daily_enforce": "1"}, headers=live)
    assert "Settings saved" in off.text and "being updated" in off.text
    assert not re.search(r'name="tracking_enabled"[^>]*checked', off.text)
    async with db.session() as session:
        assert (await session.get(Organization, owner.org_id)).tracking_enabled is False
        latest = (
            (await session.execute(select(Deployment).order_by(Deployment.created_at.desc())))
            .scalars()
            .first()
        )
    assert latest.trigger == DeploymentTrigger.SYNC
    assert f"/app/deployments/{latest.id}" in off.text

    await run_jobs()
    html = mail.signature("admin@acme.test")
    assert 'href="https://acme.test"' in html and "/c/" not in html
    # Links in emails already sent keep working.
    assert (await client.get(f"/c/{link.token}", headers=BROWSER)).status_code == 302

    on = await client.post(
        "/app/settings", data={"daily_enforce": "1", "tracking_enabled": "1"}, headers=live
    )
    assert "being updated" in on.text
    await run_jobs()
    assert {token for token, _ in TRACKED.findall(mail.signature("admin@acme.test"))} == {
        link.token
    }
    assert len(await rows(db, TrackedLink)) == 1


async def test_saving_settings_without_a_change_does_not_touch_mailboxes(
    client, db, mail, live, run_jobs
):
    writes = len(mail.writes)
    response = await client.post(
        "/app/settings", data={"daily_enforce": "1", "tracking_enabled": "1"}, headers=live
    )
    assert "Settings saved" in response.text and "being updated" not in response.text
    await run_jobs()
    assert len(mail.writes) == writes
    assert len(await rows(db, Deployment)) == 1


async def test_tracking_toggle_before_anything_is_live_starts_nothing(client, db, designing):
    response = await client.post("/app/settings", data={}, headers=designing)
    assert "Settings saved" in response.text and "being updated" not in response.text
    assert await rows(db, Deployment) == []


async def test_nothing_is_counted_when_tracking_was_off_at_apply(
    client, db, mail, designing, run_jobs
):
    await client.post("/app/settings", data={}, headers=designing)
    await client.post(APPLY, data=form(), headers=designing)
    await run_jobs()
    assert await rows(db, TrackedLink) == []
    assert 'href="https://acme.test"' in mail.signature("admin@acme.test")


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("http://127.0.0.1:8000", False),
        ("http://localhost:8000", False),
        ("https://go.footerpilot.example", True),
    ],
)
def test_tracking_is_public_only_with_a_reachable_address(address, public):
    assert Settings(_env_file=None, tracking_base_url=address).tracking_is_public is public


async def test_pages_warn_when_counted_links_only_work_on_this_computer(client, live):
    # The test server's address is a loopback one, like a developer's machine.
    for path in ("/app/designer", "/app/settings", "/app/banner"):
        assert "only be reached from this computer" in (await client.get(path)).text
    await client.post("/app/settings", data={}, headers=live)
    assert "only be reached from this computer" not in (await client.get("/app/designer")).text


# --- Analytics ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peak", "expected"),
    [
        (0, [0, 1, 2, 3, 4]),
        (3, [0, 1, 2, 3, 4]),
        (5, [0, 2, 4, 6]),
        (8, [0, 2, 4, 6, 8]),
        (9, [0, 5, 10]),
        (37, [0, 10, 20, 30, 40]),
        (100, [0, 50, 100]),
        (101, [0, 50, 100, 150]),
        (1234, [0, 500, 1000, 1500]),
        (20000, [0, 5000, 10000, 15000, 20000]),
    ],
)
def test_axis_ticks_are_round_and_cover_the_peak(peak, expected):
    assert axis_ticks(peak) == expected


async def test_analytics_counts_per_day_and_per_link(db, owner, live):
    (link,) = await rows(db, TrackedLink)
    today = date(2026, 11, 20)
    noon = datetime(2026, 11, 20, 12, tzinfo=UTC)
    await add_clicks(db, link, noon, 3)
    await add_clicks(db, link, noon - timedelta(days=2), 5)
    await add_clicks(db, link, noon - timedelta(days=2), 4, bot=True)
    await add_clicks(db, link, noon - timedelta(days=6), 1)
    await add_clicks(db, link, noon - timedelta(days=7), 10)  # outside a 7-day range
    # The edges of a UTC day.
    await add_clicks(db, link, datetime(2026, 11, 19, 23, 59, 59, tzinfo=UTC), 1)
    await add_clicks(db, link, datetime(2026, 11, 19, 0, 0, 0, tzinfo=UTC), 1)

    async with db.session() as session:
        week = await analytics_service.load(session, owner.org_id, 7, today=today)
        month = await analytics_service.load(session, owner.org_id, 30, today=today)

    assert week.total == 11 and month.total == 21
    assert [item.day for item in week.per_day] == [date(2026, 11, 14 + n) for n in range(7)]
    assert [item.clicks for item in week.per_day] == [1, 0, 0, 0, 5, 2, 3]
    assert week.axis_ticks == [0, 2, 4, 6]
    assert [item.percent for item in week.per_day][-3:] == [83.33, 33.33, 50.0]
    assert (week.busiest.day, week.busiest.clicks) == (date(2026, 11, 18), 5)
    assert all(item.show_label for item in week.per_day)

    assert len(month.per_day) == 30
    assert month.per_day[-1].show_label and month.per_day[-1].day == today
    assert sum(item.show_label for item in month.per_day) == 6
    assert [(item.label, item.clicks) for item in month.links] == [("Website", 21)]


async def test_analytics_with_no_clicks(db, owner, live):
    async with db.session() as session:
        (link,) = (await session.execute(select(TrackedLink))).scalars()
        key = (link.kind, link.ref, link.target_url)
        stats = await analytics_service.load(session, owner.org_id, 90, in_use=frozenset({key}))
        bare = await analytics_service.load(session, owner.org_id, 90)
    assert stats.total == 0 and stats.busiest is None
    assert len(stats.per_day) == 90 and {item.percent for item in stats.per_day} == {0.0}
    # A link in the live signature is listed before anyone clicks it.
    assert [(item.label, item.clicks) for item in stats.links] == [("Website", 0)]
    assert bare.links == []


async def test_analytics_page(client, db, live):
    (link,) = await rows(db, TrackedLink)
    for _ in range(3):
        await client.get(f"/c/{link.token}", headers=BROWSER)
    await client.get(f"/c/{link.token}", headers={"User-Agent": "Googlebot"})

    page = (await client.get("/app/analytics")).text
    assert "Clicks in the last 30 days" in page
    assert re.search(r"text-5xl[^>]*>3</p>", page)
    assert 'aria-current="page"' in page and "Last 90 days" in page
    # The chart: one focusable column per day, each with its value in words.
    assert page.count('<li tabindex="0"') == 30
    today = datetime.now(UTC).strftime("%A %d %B")
    assert f'aria-label="{today}: 3 clicks"' in page
    assert "Show these numbers as a table" in page
    # The links table.
    assert re.search(r"Website\s*</td>", page) and "https://acme.test" in page
    assert "mail security" in page

    week = (await client.get("/app/analytics?days=7")).text
    assert "Clicks in the last 7 days" in week and week.count('<li tabindex="0"') == 7


@pytest.mark.parametrize("days", ["", "abc", "0", "-7", "8", "365", "7.5", "7;drop"])
async def test_analytics_falls_back_to_thirty_days(client, live, days):
    response = await client.get("/app/analytics", params={"days": days})
    assert response.status_code == 200
    assert "Clicks in the last 30 days" in response.text


async def test_analytics_before_any_click(client, live):
    page = (await client.get("/app/analytics")).text
    assert "No clicks in this period yet" in page
    assert '<li tabindex="0"' not in page
    assert re.search(r"Website\s*</td>", page)  # the live link is listed with 0


async def test_analytics_says_when_counting_is_off(client, live):
    assert "switched off" not in (await client.get("/app/analytics")).text
    await client.post("/app/settings", data={}, headers=live)
    assert "Click counting is switched off" in (await client.get("/app/analytics")).text


async def test_analytics_is_scoped_to_the_organisation(app, client, db, owner, live, make_owner):
    (link,) = await rows(db, TrackedLink)
    for _ in range(4):
        await client.get(f"/c/{link.token}", headers=BROWSER)

    globex = await make_owner("admin@globex.test")
    async with db.session() as session:
        theirs = await analytics_service.load(session, globex.org_id, 30)
        ours = await analytics_service.load(session, owner.org_id, 30)
    assert (theirs.total, theirs.links) == (0, [])
    assert ours.total == 4

    sign_in(client, globex)
    page = (await client.get("/app/analytics")).text
    assert "No clicks in this period yet" in page
    assert "acme.test" not in page


async def test_analytics_and_banner_pages_need_a_connected_organisation(client, make_user):
    for path in ("/app/analytics", "/app/banner"):
        client.cookies.clear()
        assert (await client.get(path)).headers["location"] == "/signup"
        sign_in(client, await make_user(f"someone@pending{len(path)}.test"))
        assert (await client.get(path)).headers["location"] == "/app/setup"


async def test_navigation_links_to_the_new_pages(client, live):
    page = (await client.get("/app")).text
    assert 'href="/app/banner"' in page and 'href="/app/analytics"' in page
