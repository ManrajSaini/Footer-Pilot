import pytest
from pydantic import ValidationError

from app.signatures.schema import MAX_SOCIALS, SignatureDesign


def design(**overrides) -> SignatureDesign:
    return SignatureDesign.model_validate(overrides)


def test_defaults_are_valid():
    result = design()
    assert result.schema_version == 1
    assert result.template == "classic"
    assert result.socials == []


def test_round_trips_through_json():
    original = design(
        template="photo",
        company={"name": "Acme", "website_url": "https://acme.com"},
        socials=[{"network": "x", "url": "https://x.com/acme"}],
    )
    assert SignatureDesign.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize("color", ["red", "#12345", "#12345g", "1d4ed8", "#1d4ed8ff", "", None])
def test_invalid_colours_are_rejected(color):
    with pytest.raises(ValidationError):
        design(brand={"primary_color": color})


def test_colours_are_normalised_to_lowercase():
    assert design(brand={"primary_color": " #1D4ED8 "}).brand.primary_color == "#1d4ed8"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/file",
        "mailto:someone@example.com",
        "file:///etc/passwd",
        "https://",
        "https://exa mple.com",
    ],
)
def test_invalid_url_schemes_are_rejected(url):
    with pytest.raises(ValidationError):
        design(company={"website_url": url})
    with pytest.raises(ValidationError):
        design(socials=[{"network": "linkedin", "url": url}])


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("http://example.com", "http://example.com"),
        ("example.com", "https://example.com"),
        ("  www.example.com/team  ", "https://www.example.com/team"),
        ("example.com:8080/path", "https://example.com:8080/path"),
    ],
)
def test_urls_are_normalised(given, expected):
    assert design(company={"website_url": given}).company.website_url == expected


def test_blank_optional_url_becomes_none():
    assert design(company={"website_url": "   "}).company.website_url is None
    assert design(logo={"link_url": ""}).logo.link_url is None


def test_social_url_is_required():
    with pytest.raises(ValidationError):
        design(socials=[{"network": "linkedin", "url": ""}])


def test_unknown_social_network_is_rejected():
    with pytest.raises(ValidationError):
        design(socials=[{"network": "myspace", "url": "https://example.com"}])


def test_too_many_socials_are_rejected():
    socials = [{"network": "x", "url": "https://x.com/acme"}] * (MAX_SOCIALS + 1)
    with pytest.raises(ValidationError):
        design(socials=socials)


@pytest.mark.parametrize(
    "payload",
    [
        {"template": "fancy"},
        {"brand": {"font_family": "Comic Sans MS"}},
        {"brand": {"font_size": "huge"}},
        {"logo": {"width_px": 59}},
        {"logo": {"width_px": 201}},
        {"schema_version": 2},
        {"company": {"name": "x" * 81}},
    ],
)
def test_out_of_range_values_are_rejected(payload):
    with pytest.raises(ValidationError):
        design(**payload)


@pytest.mark.parametrize(
    "payload",
    [{"unexpected": 1}, {"brand": {"background": "#ffffff"}}, {"company": {"motto": "x"}}],
)
def test_unknown_keys_are_rejected(payload):
    with pytest.raises(ValidationError):
        design(**payload)


def test_text_is_cleaned():
    company = design(
        company={"name": "  Acme\n\tInc  ", "tagline": "Tools\x00 for   teams"}
    ).company
    assert company.name == "Acme Inc"
    assert company.tagline == "Tools for teams"
