"""Offline resolution of Technocore-compatible Ed25519 did:key identifiers."""

from .resolver import DidResolutionError, resolve

__all__ = ["DidResolutionError", "resolve"]
