"""ArchPilotApp: the retro terminal interface (Textual + Rich) over the existing services."""

import getpass
import os
import uuid
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from app.tui.backend import Backend, ProjectRow, TuiError
from app.tui.screens import (
    ChecklistScreen,
    CIScreen,
    HelpScreen,
    MissionScreen,
    PaletteScreen,
    PatchScreen,
    RunScreen,
    TasksScreen,
)
from app.tui.theme import DEFAULT_THEME, MONO_THEME, THEMES, UnknownThemeError, no_color

CSS = """
Screen { background: $background; }
#title { color: $accent; text-style: bold; padding: 0 1; }
.frame { border: heavy $primary; border-title-color: $accent; border-title-style: bold; }
.frame:focus, .frame:focus-within { border: heavy $accent; }
DataTable { height: auto; max-height: 20; }
#statusbar { height: 1; dock: bottom; margin-bottom: 1; }
#status { width: auto; background: $background; }
#run-scroll, #diff-scroll { height: 1fr; }
ConfirmScreen, PaletteScreen, HelpScreen { align: center middle; }
#confirm, #palette, #help {
    width: 90; height: auto; border: double $warning; background: $surface; padding: 1 2;
}
#confirm-title { color: $warning; text-style: bold; }
#confirm-buttons { height: auto; }
"""


class ArchPilotApp(App[None]):
    TITLE = "ArchPilotAI"
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("colon", "palette", "Command", key_display=":"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        backend: Backend,
        *,
        project: str | None = None,
        task: str | None = None,
        resume: uuid.UUID | None = None,
        theme: str = DEFAULT_THEME,
        animation: bool = True,
        change: str | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.animation = animation
        self.project: ProjectRow | None = None
        self.plan_id: uuid.UUID | None = None
        self._start_project = project
        self._start_task = task
        self._start_resume = resume
        self._start_change = change
        for t in THEMES.values():
            self.register_theme(t)
        self.theme = theme

    def on_mount(self) -> None:
        self.push_screen(MissionScreen())
        self.call_after_refresh(self._open_start)

    def _open_start(self) -> None:
        try:
            if self._start_resume is not None:
                project_id, plan_id, task_key = self.backend.find_run(self._start_resume)
                self.project = next(p for p in self.backend.projects() if p.id == project_id)
                self.plan_id = plan_id
                self.push_screen(RunScreen(project_id, self._start_resume))
                return
            if self._start_project is not None:
                self.project = self.backend.find_project(self._start_project)
                mission = self.screen
                if isinstance(mission, MissionScreen):
                    mission.show(self.project)
                if self._start_task is not None:
                    self.open_checklist(self._start_task)
                if self._start_change is not None:
                    self.submit_change(self._start_change)
        except TuiError as exc:
            self.notify(str(exc), title="Error", severity="error", markup=False)

    def submit_change(self, text: str) -> None:
        if self.project is None:
            self.notify("Select a project first.", severity="warning", markup=False)
            return
        view = self.backend.submit_change(self.project.id, text, getpass.getuser())
        affected = ", ".join(view.components) or "none named"
        self.notify(
            f"{view.key}: {view.impact_class}. {view.reason} Affected components: {affected}. "
            "Approve it on the Changes page.",
            title="Change request",
            markup=False,
            timeout=15,
        )

    # ---- navigation (used by the screens and the palette) ----------------------------------

    def _need_plan(self) -> bool:
        if self.project is None:
            self.notify("Select a project first.", severity="warning", markup=False)
            return False
        if self.plan_id is None:
            self.notify(
                "This project has no approved implementation plan.",
                severity="warning",
                markup=False,
            )
            return False
        return True

    def open_tasks(self) -> None:
        if self._need_plan():
            assert self.project is not None and self.plan_id is not None
            self.push_screen(TasksScreen(self.project.id, self.plan_id))

    def open_checklist(self, task_key: str) -> None:
        if self._need_plan():
            assert self.project is not None and self.plan_id is not None
            self.push_screen(ChecklistScreen(self.project.id, self.plan_id, task_key.upper()))

    def open_run(self, run_id: uuid.UUID) -> None:
        if self.project is not None:
            self.push_screen(RunScreen(self.project.id, run_id))

    def open_patch(self, patch_id: uuid.UUID) -> None:
        if self.project is not None:
            self.push_screen(PatchScreen(self.project.id, patch_id))

    def open_ci(self, run_id: uuid.UUID) -> None:
        if self.project is not None:
            self.push_screen(CIScreen(self.project.id, run_id))

    # ---- palette and help ------------------------------------------------------------------

    def action_palette(self) -> None:
        self.push_screen(PaletteScreen(), self.run_command)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def run_command(self, command: str | None) -> None:
        if not command:
            return
        name, _, arg = command.lstrip(":").strip().partition(" ")
        name, arg = name.lower(), arg.strip()
        try:
            if name in ("quit", "q", "exit"):
                self.exit()
            elif name == "help":
                self.action_help()
            elif name == "back":
                if len(self.screen_stack) > 2:
                    self.pop_screen()
            elif name == "mission":
                while len(self.screen_stack) > 2:
                    self.pop_screen()
            elif name == "tasks":
                self.open_tasks()
            elif name == "checklist" and arg:
                self.open_checklist(arg)
            elif name in ("run", "patch", "ci") and arg:
                target = uuid.UUID(arg)
                {"run": self.open_run, "patch": self.open_patch, "ci": self.open_ci}[name](target)
            elif name == "change" and arg:
                self.submit_change(arg)
            elif name == "theme":
                self.set_theme(arg)
            else:
                self.notify(f"Unknown command: {command}", severity="warning", markup=False)
        except ValueError:
            self.notify(f"Not a valid id: {arg}", severity="error", markup=False)

    def set_theme(self, name: str) -> None:
        if name not in THEMES:
            self.notify(str(UnknownThemeError(name)), severity="error", markup=False)
            return
        if no_color(os.environ) and name != MONO_THEME:
            self.notify("NO_COLOR is set: only the mono theme is used.", markup=False)
            return
        self.theme = name
