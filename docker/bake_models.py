"""Download every model weight into the image so a cold pod is not minutes of HuggingFace."""

import subprocess
import sys

from huggingface_hub import snapshot_download

from src.core.config import SETTINGS

HF_HUB = sys.argv[1]
ARTIFACTS = sys.argv[2]

# The embedder, the BLIP captioner and the tokenizer resolve through the ambient HF cache
# ($HF_HOME/hub); the reranker and docling are handed SETTINGS.paths.models directly.
for name in (SETTINGS.models.embedder_name, SETTINGS.models.blip_name):
    snapshot_download(name, cache_dir=HF_HUB)
snapshot_download(SETTINGS.models.reranker_name, cache_dir=ARTIFACTS)
subprocess.run(["docling-tools", "models", "download", "-o", ARTIFACTS], check=True)
