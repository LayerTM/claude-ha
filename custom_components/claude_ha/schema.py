"""The schema library Home Assistant validates with.

Home Assistant 2026.9 replaced voluptuous with probatio, and its typing
follows from 2026.10, so a voluptuous schema is no longer the type Home
Assistant accepts or raises. Older releases only ship voluptuous. Every
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
