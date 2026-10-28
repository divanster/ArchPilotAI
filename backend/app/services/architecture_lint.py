import logging
import uuid
from typing import Any

from app.core.config import Settings
from app.domain.architecture import ArchitectureContent
from app.domain.semantic_lint import LintConfig, RequirementText, lint_proposal
from app.repositories.requirement import RequirementRepository

logger = logging.getLogger(__name__)


def lint_config_from_settings(settings: Settings) -> LintConfig:
    return LintConfig().with_extra_terms(settings.lint_extra_product_terms)


def compute_lint(
    requirements: RequirementRepository,
    project_id: uuid.UUID,
    title: str,
    summary: str,
    content: ArchitectureContent,
    config: LintConfig | None,
) -> dict[str, Any] | None:
    """Lint report as stored JSON. Advisory only: a lint bug must never lose a proposal, so
    any failure is logged (no content) and the proposal is stored without findings."""
    try:
        texts = {
            r.id: RequirementText(r.title, r.description, r.source_quote or "")
            for r in requirements.list(project_id, "confirmed")
        }
        return lint_proposal(title, summary, content, texts, config).model_dump(mode="json")
    except Exception:
        logger.exception("semantic lint failed project=%s", project_id)
        return None
