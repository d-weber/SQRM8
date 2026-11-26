class Provider:
    source_name: str = ""

    def poll(self, now: float) -> None:
        pass

    def contribute(self, state: dict) -> None:
        pass

    def drain_events(self) -> list[dict]:
        return []

    def on_device_message(self, msg: dict) -> None:
        pass

    def focus_info(self, sid: str) -> dict | None:
        """Optional session metadata ({sid, pid, cwd, source, surface}) for device 'focus' actions."""
        return None

    def hook_covered_kinds(self) -> set[str] | None:
        """Event kinds authoritative from hooks for this provider (None = default HOOK_KINDS)."""
        return None
