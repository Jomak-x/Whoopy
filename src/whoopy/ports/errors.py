"""Backend-neutral errors used by every Whoopy model adapter."""

from __future__ import annotations


class AdapterError(RuntimeError):
    """Base class for a failure deliberately classified by an adapter."""


class TransientAdapterError(AdapterError):
    """A temporary failure that a bounded retry may resolve."""


class FatalAdapterError(AdapterError):
    """A deterministic setup, compatibility, or input failure."""


class InvalidAdapterOutput(FatalAdapterError):
    """A backend completed but returned output that violates its port contract."""
