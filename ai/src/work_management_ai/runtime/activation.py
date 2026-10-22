"""Trusted, immutable optional-capability state, never inferred from model input."""

from dataclasses import dataclass

WEB_CAPABILITIES = frozenset({"retrieval.search_web", "retrieval.summarize_web"})
WEB_TOOLS = frozenset({"web.search@1", "web.read_pages@1", "web.revalidate@1"})


@dataclass(frozen=True, slots=True)
class ActivationContext:
    active_phase: int
    core_exit_verified: bool = False
    active_tracks: frozenset[str] = frozenset()
    provider_ready: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.active_phase) is not int
            or not 2 <= self.active_phase <= 99
            or type(self.core_exit_verified) is not bool
            or type(self.provider_ready) is not bool
            or type(self.active_tracks) is not frozenset
            or any(type(track) is not str for track in self.active_tracks)
        ):
            raise ValueError("INVALID_ACTIVATION_CONTEXT")


def capability_enabled(capability: str, activation: ActivationContext | None) -> bool:
    """Only the explicitly declared web slice is eligible; unknown capabilities deny."""
    return (
        isinstance(activation, ActivationContext)
        and capability in WEB_CAPABILITIES
        and activation.active_phase >= 5
        and activation.core_exit_verified
        and "I3" in activation.active_tracks
        and activation.provider_ready
    )
