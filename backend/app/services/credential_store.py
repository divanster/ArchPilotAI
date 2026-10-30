"""The OS credential store for personal access tokens (via keyring).

The token lives only there. Callers keep the reference, never the value. Failures never carry the
token in their message.
"""

from typing import Protocol

SERVICE_NAME = "ArchPilotAI"


class CredentialStoreUnavailableError(Exception):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "No usable OS credential store (keyring) is available, so the access token was not "
            "saved. Use the Git credential manager or SSH instead, or install a keyring backend."
            + (f" ({detail})" if detail else "")
        )


class CredentialStore(Protocol):
    def available(self) -> bool: ...

    def set(self, reference: str, secret: str) -> None: ...

    def get(self, reference: str) -> str | None: ...

    def delete(self, reference: str) -> None: ...


class KeyringCredentialStore:
    def available(self) -> bool:
        try:
            import keyring
            from keyring.backends import fail, null

            backend = keyring.get_keyring()
        except Exception:
            return False
        return not isinstance(backend, fail.Keyring | null.Keyring)

    def set(self, reference: str, secret: str) -> None:
        if not self.available():
            raise CredentialStoreUnavailableError
        import keyring

        try:
            keyring.set_password(SERVICE_NAME, reference, secret)
        except Exception as exc:
            raise CredentialStoreUnavailableError(type(exc).__name__) from None

    def get(self, reference: str) -> str | None:
        if not self.available():
            raise CredentialStoreUnavailableError
        import keyring

        try:
            return keyring.get_password(SERVICE_NAME, reference)
        except Exception as exc:
            raise CredentialStoreUnavailableError(type(exc).__name__) from None

    def delete(self, reference: str) -> None:
        if not self.available():
            return
        import keyring

        try:
            keyring.delete_password(SERVICE_NAME, reference)
        except Exception:  # already gone, or the store refuses; nothing more to do
            return
