from __future__ import annotations


class SchedulingError(ValueError):
    """Base scheduling failure. Subclasses ValueError so existing
    `except ValueError` callers keep working; the detail string is the wire
    contract matched by the frontend (e.g. "timing_locked",
    "tiktok_precedence_displaced:<titles>")."""

    http_status = 422


class SchedulingConflictError(SchedulingError):
    http_status = 409


class SchedulingLockedError(SchedulingError):
    http_status = 423


class SchedulingNotFoundError(SchedulingError):
    http_status = 404
