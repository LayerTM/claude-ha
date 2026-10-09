"""The schema library Home Assistant validates with.

From Home Assistant 2026.10 its signatures are typed with probatio, so
type-checking needs probatio's ``Schema`` and ``Invalid``. At runtime Home
Assistant installs probatio as ``voluptuous`` (2026.9+), so both names are
the same objects there; releases before that only ship voluptuous. Every
module takes ``vol`` from here, so the choice is made in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import probatio as vol
else:
    try:
        import probatio as vol
    except ImportError:
        import voluptuous as vol

__all__ = ["vol"]
