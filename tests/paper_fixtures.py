"""Original-rule fixtures keep accounting/exit tests independent of new gates."""
from feedboard.paper import PaperConfig


def baseline_config(**changes):
    return PaperConfig(**{"entry_window_enabled": False, "confirmation_enabled": False,
                          "choppiness_enabled": False, **changes})
