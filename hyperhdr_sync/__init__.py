"""
HyperHDR Sync plugin for Music Assistant.

Makes the LEDs driven by HyperHDR (ambient lighting behind a TV or monitor) react
to the music Music Assistant plays. It uses the Sendspin visualization pipeline,
renders the light effects itself and streams one colour per LED to HyperHDR's
"UDP raw receiver".

Each configured instance is one HyperHDR LED instance that appears as a light
player in Music Assistant. Grouping that light player with a speaker makes the
LEDs react to that speaker's audio; when the music stops HyperHDR returns to
whatever it showed before (usually the screen grabber).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    from .provider import HyperHdrProvider  # noqa: PLC0415

    return HyperHdrProvider(mass, manifest, config, SUPPORTED_FEATURES)
