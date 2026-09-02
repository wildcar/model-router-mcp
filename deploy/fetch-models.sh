#!/usr/bin/env bash
# Fetch the weights of the models the `local` provider computes on this host.
#
# Idempotent: a file that is present and matches its sha256 is left alone; one
# that is missing or corrupt is (re)downloaded and checked again. Run by
# install.sh on first install and by deploy.sh on every update, so a model added
# to this list reaches production with the code that uses it.
#
# The hashes are pinned here on purpose. A model file is code the router runs
# on every request; a silent change upstream must fail loudly, not load.
#
# Usage:  bash deploy/fetch-models.sh [MODELS_DIR]
#   MODELS_DIR defaults to $LOCAL_MODELS_DIR, then ./data/models (relative to the
#   repo root). Run it as the user that owns that directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODELS_DIR="${1:-${LOCAL_MODELS_DIR:-${REPO_DIR}/data/models}}"

# One line per file: <relative path>  <sha256>  <url>
# UVDoc, grid-output ONNX export of the original weights (Apache-2.0):
# https://huggingface.co/fredcallagan/uvdoc-grid-onnx — paper: arXiv 2302.02887.
# Two files: the graph and its external fp32 weight data, which ONNX Runtime
# expects next to the graph under exactly this name.
FILES=(
  "uvdoc/UVDoc_grid.onnx       044b406398e8accf2b8c896043f22d318221e443bb095c55d184e97f3eb003f7  https://huggingface.co/fredcallagan/uvdoc-grid-onnx/resolve/main/UVDoc_grid.onnx"
  "uvdoc/UVDoc_grid.onnx.data  3a58a04944e59578200d7b0ed02ccb3ade934e9d88efa21454fbb8b53fa40f02  https://huggingface.co/fredcallagan/uvdoc-grid-onnx/resolve/main/UVDoc_grid.onnx.data"
  "uvdoc/LICENSE               309f09c5c5c72fdff9f40da1341f7ba4650bbc0c4bc3ea433de68fa57e1ff7ba  https://huggingface.co/fredcallagan/uvdoc-grid-onnx/resolve/main/LICENSE"
)

echo ">> models: ${MODELS_DIR}"
mkdir -p "${MODELS_DIR}"

check() {  # check <path> <sha256>  -> 0 if the file is there and matches
  [[ -f "$1" ]] || return 1
  echo "$2  $1" | sha256sum --check --status
}

status=0
for entry in "${FILES[@]}"; do
  read -r rel sha url <<<"${entry}"
  path="${MODELS_DIR}/${rel}"
  if check "${path}" "${sha}"; then
    echo "   ok       ${rel}"
    continue
  fi
  mkdir -p "$(dirname "${path}")"
  echo "   fetching ${rel}"
  tmp="${path}.part"
  if ! curl -fsSL --retry 3 --retry-delay 2 -o "${tmp}" "${url}"; then
    echo "ERROR: download failed: ${url}" >&2
    rm -f "${tmp}"
    status=1
    continue
  fi
  if check "${tmp}" "${sha}"; then
    mv -f "${tmp}" "${path}"
    echo "   ok       ${rel} ($(du -h "${path}" | cut -f1))"
  else
    echo "ERROR: sha256 mismatch for ${rel}; upstream file changed — not installing it" >&2
    rm -f "${tmp}"
    status=1
  fi
done

if [[ "${status}" -ne 0 ]]; then
  echo "ERROR: some model files are missing; runs on them answer model_not_loaded" >&2
fi
exit "${status}"
