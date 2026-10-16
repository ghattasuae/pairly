#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
#
# Install a pinned CI tool binary and print its path. Used by the CI workflow and by
# the `ci-*` Makefile targets, so local runs use the same versions as CI.
#
# Every download is checked against a SHA-256 written in this file (copied from the
# upstream release checksums file). A mismatch aborts. Binaries are cached outside the
# repo, in ${PAIRLY_CI_TOOLS:-${XDG_CACHE_HOME:-$HOME/.cache}/pairly-ci-tools}.
#
# Usage:
#   scripts/ci_tools.sh gitleaks     # prints the path to gitleaks
#   scripts/ci_tools.sh actionlint   # prints the path to actionlint
#
# To bump a tool: change its version and all four hashes together.

set -euo pipefail

GITLEAKS_VERSION="8.30.1"
ACTIONLINT_VERSION="1.7.12"

sha_for() {
  case "$1" in
    gitleaks_8.30.1_linux_x64.tar.gz) echo 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
    gitleaks_8.30.1_linux_arm64.tar.gz) echo e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;;
    gitleaks_8.30.1_darwin_x64.tar.gz) echo dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709 ;;
    gitleaks_8.30.1_darwin_arm64.tar.gz) echo b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
    actionlint_1.7.12_linux_amd64.tar.gz) echo 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8 ;;
    actionlint_1.7.12_linux_arm64.tar.gz) echo 325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6 ;;
    actionlint_1.7.12_darwin_amd64.tar.gz) echo 5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644 ;;
    actionlint_1.7.12_darwin_arm64.tar.gz) echo aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f ;;
    *) return 1 ;;
  esac
}

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  x86_64 | amd64) arch_go="amd64"; arch_gl="x64" ;;
  arm64 | aarch64) arch_go="arm64"; arch_gl="arm64" ;;
  *) echo "ci_tools: unsupported CPU $(uname -m)" >&2; exit 1 ;;
esac

tool="${1:-}"
case "$tool" in
  gitleaks)
    version="$GITLEAKS_VERSION"
    asset="gitleaks_${version}_${os}_${arch_gl}.tar.gz"
    url="https://github.com/gitleaks/gitleaks/releases/download/v${version}/${asset}"
    ;;
  actionlint)
    version="$ACTIONLINT_VERSION"
    asset="actionlint_${version}_${os}_${arch_go}.tar.gz"
    url="https://github.com/rhysd/actionlint/releases/download/v${version}/${asset}"
    ;;
  *)
    echo "usage: $0 gitleaks|actionlint" >&2
    exit 2
    ;;
esac

expected="$(sha_for "$asset")" || { echo "ci_tools: no pinned hash for $asset" >&2; exit 1; }

cache="${PAIRLY_CI_TOOLS:-${XDG_CACHE_HOME:-$HOME/.cache}/pairly-ci-tools}/${tool}-${version}"
bin="$cache/$tool"
if [ -x "$bin" ]; then
  echo "$bin"
  exit 0
fi

mkdir -p "$cache"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp/$asset" "$url"

if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$tmp/$asset" | cut -d' ' -f1)"
else
  actual="$(shasum -a 256 "$tmp/$asset" | cut -d' ' -f1)"
fi
if [ "$actual" != "$expected" ]; then
  echo "ci_tools: checksum mismatch for $asset (expected $expected, got $actual)" >&2
  exit 1
fi

tar -xzf "$tmp/$asset" -C "$tmp" "$tool"
install -m 0755 "$tmp/$tool" "$bin"
echo "$bin"
