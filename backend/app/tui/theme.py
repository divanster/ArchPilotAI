"""Themes, the pixel logo and text progress bars.

The retro style applies to frames and labels only; code, diffs, errors, paths and confirmations
are rendered as plain text by the screens.
"""

from collections.abc import Mapping

from textual.theme import Theme

DEFAULT_THEME = "cyber-8"
MONO_THEME = "mono"

THEMES: dict[str, Theme] = {
    "cyber-8": Theme(
        name="cyber-8",
        primary="#ff2fd6",
        secondary="#29e7ff",
        accent="#fff23a",
        warning="#ffb000",
        error="#ff4b4b",
        success="#3dff8a",
        foreground="#e8e8ff",
        background="#0d0221",
        surface="#1a0b3a",
        panel="#26115a",
        dark=True,
    ),
    "green-phosphor": Theme(
        name="green-phosphor",
        primary="#33ff66",
        secondary="#1fbf4a",
        accent="#9dff9d",
        warning="#ccff33",
        error="#ff5555",
        success="#33ff66",
        foreground="#b6ffb6",
        background="#020d02",
        surface="#061a06",
        panel="#0b260b",
        dark=True,
    ),
    "amber-terminal": Theme(
        name="amber-terminal",
        primary="#ffb000",
        secondary="#ff8c00",
        accent="#ffd27f",
        warning="#ffd000",
        error="#ff5f3f",
        success="#ffc94d",
        foreground="#ffd9a0",
        background="#120a00",
        surface="#1f1300",
        panel="#2b1b00",
        dark=True,
    ),
    "mono": Theme(
        name="mono",
        primary="#ffffff",
        secondary="#c0c0c0",
        accent="#ffffff",
        warning="#ffffff",
        error="#ffffff",
        success="#ffffff",
        foreground="#ffffff",
        background="#000000",
        surface="#000000",
        panel="#000000",
        dark=True,
    ),
}


class UnknownThemeError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown theme {name!r}; choose one of: {', '.join(THEMES)}")


def no_color(env: Mapping[str, str]) -> bool:
    """https://no-color.org: any non-empty NO_COLOR disables color."""
    return bool(env.get("NO_COLOR", ""))


def resolve_theme(requested: str | None, env: Mapping[str, str]) -> str:
    """The theme to use. NO_COLOR always wins and selects the monochrome theme."""
    if requested is not None and requested not in THEMES:
        raise UnknownThemeError(requested)
    if no_color(env):
        return MONO_THEME
    return requested or DEFAULT_THEME


LOGO = "\n".join(
    [
        "▄▀▀▄ █▀▀▄ ▄▀▀▀ █  █ █▀▀▄ ▀█▀ █   ▄▀▀▄ ▀█▀",
        "█▄▄█ █▄▄▀ █    █▀▀█ █▄▄▀  █  █   █  █  █ ",
        "█  █ █  █ ▀▄▄▄ █  █ █    ▄█▄ █▄▄ ▀▄▄▀  █ ",
    ]
)


def bar(used: float, total: float, width: int = 20) -> str:
    """A pixel progress bar: [██████░░░░]  60%. A total of 0 means "no limit"."""
    if total <= 0:
        return "[" + "░" * width + "] no limit"
    ratio = max(0.0, min(used / total, 1.0))
    filled = round(ratio * width)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {round(ratio * 100):>3d}%"


STATUS_BADGES: dict[str, str] = {
    "todo": "[ TODO ]",
    "running": "[ RUN  ]",
    "proposed": "[ PROP ]",
    "accepted": "[  OK  ]",
    "rejected": "[ REJ  ]",
    "failed": "[ FAIL ]",
    "stopped": "[ STOP ]",
    "finished": "[ DONE ]",
    "draft": "[DRAFT ]",
    "approved": "[ APPR ]",
}


def badge(status: str) -> str:
    return STATUS_BADGES.get(status, f"[{status[:6].upper():^6}]")
