"""Download every model weight into the image so a cold pod is not minutes of HuggingFace."""

import subprocess
import sys
import time
from typing import Any, Callable, TypeVar

from huggingface_hub import snapshot_download
from huggingface_hub.errors import HfHubHTTPError

from src.core.config import SETTINGS

HF_HUB = sys.argv[1]
ARTIFACTS = sys.argv[2]
# A CI runner shares its address with everyone else on it, so an anonymous pull of six repos
# hits HuggingFace's per-IP limit often enough to break the build, and the window is minutes
# rather than seconds. Set HF_TOKEN to raise the limit instead of waiting it out.
_ATTEMPTS = 8
_BACKOFF_S = 5
_MAX_BACKOFF_S = 60
_RETRIED = frozenset({429, 500, 502, 503, 504})

T = TypeVar("T")


def _retrying(call: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    for attempt in range(_ATTEMPTS):
        try:
            return call(*args, **kwargs)
        except (HfHubHTTPError, subprocess.CalledProcessError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retriable = not isinstance(exc, HfHubHTTPError) or status in _RETRIED
            if attempt == _ATTEMPTS - 1 or not retriable:
                raise
            time.sleep(min(_MAX_BACKOFF_S, _BACKOFF_S * 2**attempt))
    raise AssertionError("unreachable")


def _docling(*args: str) -> None:
    _retrying(
        subprocess.run, ["docling-tools", "models", "download", *args, "-o", ARTIFACTS], check=True
    )


# The embedder, the BLIP captioner and the tokenizer resolve through the ambient HF cache
# ($HF_HOME/hub); the reranker and docling are handed SETTINGS.paths.models directly.
for model in (SETTINGS.models.embedder_name, SETTINGS.models.blip_name):
    _retrying(snapshot_download, model, cache_dir=HF_HUB)
_retrying(snapshot_download, SETTINGS.models.reranker_name, cache_dir=ARTIFACTS)
_docling()
# RapidOCR is not part of docling's default set and is only reached when a PDF has no text
# layer, which is exactly when a network pull would surprise someone: bake it too (~30 MB).
_docling("rapidocr", "--rapidocr-backend-lang", "onnxruntime:ch")
