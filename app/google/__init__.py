"""Google gateways. Import the interfaces from `app.google.base`."""

from fastapi import Request

from app.config import Settings
from app.google.base import GoogleGateways


def build_gateways(settings: Settings) -> GoogleGateways:
    """Real or fake gateways, according to GOOGLE_MODE."""
    if settings.google_mode == "fake":
        from app.google.fake import FakeDirectoryGateway, FakeGmailGateway

        return GoogleGateways(directory=FakeDirectoryGateway(), gmail=FakeGmailGateway())

    # Imported here so fake mode never loads Google's client libraries.
    from app.google.real import RealDirectoryGateway, RealGmailGateway, load_service_account_info

    info = load_service_account_info(settings.google_service_account_json.get_secret_value())
    return GoogleGateways(directory=RealDirectoryGateway(info), gmail=RealGmailGateway(info))


def get_google(request: Request) -> GoogleGateways:
    """FastAPI dependency. Tests swap `app.state.google` for their own fakes."""
    return request.app.state.google
