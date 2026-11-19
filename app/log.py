"""Logging setup.

Convention: put context in the message as key=value pairs (org_id=..., deployment_id=...).
Never log signature HTML, tokens or keys.
"""

import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
