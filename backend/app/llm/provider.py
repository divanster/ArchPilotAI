from typing import Protocol


class LLMProvider(Protocol):
    """Abstraction for all LLM access. Workbench will later be one implementation.

    No implementation exists yet; the interface is finalised in a later step.
    """

    def complete(self, prompt: str) -> str: ...
