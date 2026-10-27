class LLMError(Exception):
    """Base class. `public_message` is safe to show to clients; it never carries upstream bodies."""

    public_message = "The language model request failed."

    def __init__(
        self, public_message: str | None = None, *, status_code: int | None = None
    ) -> None:
        self.public_message = public_message or type(self).public_message
        self.status_code = status_code  # upstream HTTP status, if any (safe to report)
        super().__init__(self.public_message)


class LLMConfigurationError(LLMError):
    public_message = "The language model provider is not configured."


class LLMAuthenticationError(LLMError):
    public_message = "The language model provider rejected the credentials or blocked the request."


class LLMRateLimitError(LLMError):
    public_message = "The language model provider is rate limiting requests."


class LLMTimeoutError(LLMError):
    public_message = "The language model provider did not answer in time."


class LLMUnavailableError(LLMError):
    public_message = "The language model provider is temporarily unavailable."


class LLMRequestRejectedError(LLMError):
    public_message = "The language model provider rejected the request."


class LLMInvalidResponseError(LLMError):
    public_message = "The language model provider returned an unexpected response."
