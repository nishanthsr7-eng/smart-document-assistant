class DocumentError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class EncryptedDocument(DocumentError):
    def __init__(self) -> None:
        super().__init__("This PDF is password-protected. Remove the password and re-upload.")


class EmptyDocument(DocumentError):
    def __init__(self) -> None:
        super().__init__("No text could be extracted from this document.")


class ScannedDocument(DocumentError):
    def __init__(self, reason: str = "OCR could not read any text from it.") -> None:
        super().__init__(f"This looks like a scanned document with no text layer. {reason}")


class UnsupportedFormat(DocumentError):
    def __init__(self, extension: str) -> None:
        super().__init__(f"'.{extension}' files aren't supported. Upload a PDF or TXT file.")


class DocumentTooLarge(DocumentError):
    def __init__(self, max_mb: int) -> None:
        super().__init__(f"File exceeds the {max_mb} MB upload limit.")


class DocumentTooManyPages(DocumentError):
    def __init__(self, max_pages: int) -> None:
        super().__init__(f"Document exceeds the {max_pages}-page limit.")


class DocumentNotFound(Exception):
    """No such document in the caller's tenant. Deliberately indistinguishable from a
    document that exists in another tenant: existence is itself tenant-scoped information.
    """

    def __init__(self, doc_id: str) -> None:
        self.message = f"No document '{doc_id}'."
        super().__init__(self.message)


class ModelUnavailable(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class GenerationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ConfigError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class StorageError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class AuthError(Exception):
    """Caller is not authenticated: no token, bad token, or bad credentials."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class PermissionDenied(Exception):
    """Caller is authenticated but lacks the role, or the resource is another tenant's."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
