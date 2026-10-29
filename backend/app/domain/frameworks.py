"""Framework rules the server enforces on generated code. Only Django has one so far.

Django migrations are produced by `makemigrations`, never written by the model. The app label is
never assumed: it is read from the project decisions ("app label: shop") or asked for in the notes.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

_MIGRATION = re.compile(r"(?:^|/)migrations/(?!__init__\.py$)[^/]+\.py$")
_LABEL = re.compile(r"app[ _-]?label\s*(?:[:=]|\bis\b)\s*[`'\"]?([A-Za-z_]\w*)", re.IGNORECASE)


def is_migration_path(path: str) -> bool:
    return bool(_MIGRATION.search(path))


def app_label_in(texts: Iterable[str]) -> str | None:
    for text in texts:
        match = _LABEL.search(text)
        if match:
            return match.group(1)
    return None


@dataclass(frozen=True)
class FrameworkRules:
    django: bool = False
    app_label: str | None = None

    @classmethod
    def detect(
        cls, backend_choices: Iterable[str], decision_texts: Iterable[str], paths: Iterable[str]
    ) -> "FrameworkRules":
        django = any("django" in c.lower() for c in backend_choices) or any(
            p.rsplit("/", 1)[-1] == "manage.py" for p in paths
        )
        return cls(django, app_label_in(decision_texts) if django else None)

    def refuses(self, path: str) -> bool:
        return self.django and is_migration_path(path)

    def command(self) -> str:
        return f"python manage.py makemigrations {self.app_label or '<app_label>'}"

    def note(self) -> str:
        base = f"Django migrations are not written by hand: run `{self.command()}` instead."
        if self.app_label:
            return base
        return base + " The app label is not recorded in the project decisions; please state it."

    def context(self) -> list[str]:
        if not self.django:
            return []
        label = (
            f"The app label is {self.app_label}."
            if self.app_label
            else "The app label is unknown: do not guess it; ask for it in the notes."
        )
        return [
            "Django: do not write or edit files under migrations/. Change the models and add a "
            f"note 'run makemigrations'. {label}"
        ]
