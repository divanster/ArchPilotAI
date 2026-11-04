"""Small retro widgets. Frames and labels are styled; content is always plain text."""

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from app.tui.theme import LOGO


class Plain(Static):
    """Plain, fully readable text: never parsed as markup, never styled by the retro theme.

    Code, diffs, errors, paths and confirmations go through this widget. `text` keeps the
    last value shown (tests and screen readers read it).
    """

    DEFAULT_CSS = """
    Plain { color: $foreground; background: $surface; padding: 0 1; }
    """

    def __init__(
        self, text: str = "", *, id: str | None = None, classes: str = "", focusable: bool = False
    ) -> None:
        super().__init__(Text(text), id=id, classes=classes, markup=False)
        self.can_focus = focusable
        self.text = text

    def set(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self.update(Text(text))


class Logo(Static):
    DEFAULT_CSS = """
    Logo { color: $primary; text-style: bold; padding: 0 1; height: auto; }
    """

    def __init__(self) -> None:
        super().__init__(Text(LOGO), markup=False)


class Cursor(Static):
    """The blinking block cursor of the status line; steady with --no-animation."""

    DEFAULT_CSS = """
    Cursor { width: 1; color: $accent; }
    """

    lit: reactive[bool] = reactive(True)

    def __init__(self, animate: bool) -> None:
        super().__init__("█", markup=False)
        self.blinking = animate

    def on_mount(self) -> None:
        if self.blinking:
            self.set_interval(0.5, self._blink)

    def _blink(self) -> None:
        self.lit = not self.lit

    def watch_lit(self, lit: bool) -> None:
        self.update("█" if lit else " ")


def frame(widget: Static, label: str) -> Static:
    """A box frame with a pixel label, e.g. ▓ PATCH ▓ (style only; the content stays plain)."""
    widget.add_class("frame")
    widget.border_title = f"▓ {label.upper()} ▓"
    return widget
