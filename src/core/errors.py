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
    def __init__(self) -> None:
        super().__init__("This looks like a scanned document with no text layer. OCR isn't supported.")


class UnsupportedFormat(DocumentError):
    def __init__(self, extension: str) -> None:
        super().__init__(f"'.{extension}' files aren't supported. Upload a PDF or TXT file.")


class DocumentTooLarge(DocumentError):
    def __init__(self, max_mb: int) -> None:
        super().__init__(f"File exceeds the {max_mb} MB upload limit.")


class DocumentTooManyPages(DocumentError):
    def __init__(self, max_pages: int) -> None:
        super().__init__(f"Document exceeds the {max_pages}-page limit.")


class ModelUnavailable(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class GenerationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
