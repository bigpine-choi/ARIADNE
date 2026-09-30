"""Outlook delegated connection: Microsoft login, encrypted persistence, read-only mail."""

from .errors import OutlookError
from .service import OutlookService

__all__ = ["OutlookError", "OutlookService"]
