"""HTTP server — dark dashboard + JSON API + Stripe endpoints at :8420."""
from .app import serve

__all__ = ["serve"]
