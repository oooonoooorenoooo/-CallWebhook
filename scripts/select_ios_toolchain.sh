#!/usr/bin/env bash
# Keep all iPhone builds on the same stable, explicitly selected toolchain.
set -euo pipefail

: "${GITHUB_ENV:?This selector must run in GitHub Actions}"
export DEVELOPER_DIR=/Applications/Xcode_27.2.app/Contents/Developer
if [[ ! -d "$DEVELOPER_DIR" ]]; then
  echo '::error::Xcode 27.2 is required. Use the xcode-27 runner; do not fall back to the runner default.'
  exit 1
fi

toolchain_version="$(xcodebuild -version)"
sdk_version="$(xcrun --sdk iphoneos --show-sdk-version)"
printf '%s\niPhoneOS SDK %s\n' "$toolchain_version" "$sdk_version"
if [[ "${toolchain_version%%$'\n'*}" != 'Xcode 27.2' || "$sdk_version" != '27.2' ]]; then
  echo '::error::Expected stable Xcode 27.2 with iPhoneOS SDK 27.2.'
  exit 1
fi

printf 'DEVELOPER_DIR=%s\n' "$DEVELOPER_DIR" >> "$GITHUB_ENV"
xcrun swift --version
