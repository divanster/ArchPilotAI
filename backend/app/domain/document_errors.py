class DocumentError(Exception):
    """Base class for document validation errors."""


class UnsupportedDocumentTypeError(DocumentError):
    pass


class DocumentTooLargeError(DocumentError):
    pass


class InvalidDocumentError(DocumentError):
    pass
