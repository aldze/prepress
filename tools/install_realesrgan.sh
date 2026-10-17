#!/bin/bash
# Ставит движок ИИ-апскейла Real-ESRGAN.
#
# В репозиторий бинарник не кладём: 58 МБ, платформенно-зависимый, да и в
# облачной папке ему делать нечего. Ставится в Application Support — вне
# синхронизируемых каталогов, отдельно для каждой машины.
set -euo pipefail

DEST="$HOME/Library/Application Support/prepress_automation/tools/realesrgan"
URL="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip"

if [ "${1:-}" = "--check" ]; then
  if [ -x "$DEST/realesrgan-ncnn-vulkan" ] && [ -f "$DEST/models/realesrgan-x4plus.bin" ]; then
    echo "Real-ESRGAN: установлен ($DEST)"
    exit 0
  fi
  echo "Real-ESRGAN: не установлен"
  exit 1
fi

if [ -x "$DEST/realesrgan-ncnn-vulkan" ] && [ "${1:-}" != "--force" ]; then
  echo "Уже установлен: $DEST"
  echo "Переустановить: $0 --force"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Скачиваю Real-ESRGAN (~49 МБ)…"
curl -fL --progress-bar -o "$TMP/r.zip" "$URL"

echo "Распаковываю…"
unzip -q -o "$TMP/r.zip" -d "$TMP/unpacked"

mkdir -p "$DEST/models"
cp "$TMP/unpacked/realesrgan-ncnn-vulkan" "$DEST/"
# Только фотомодель: аниме-модели и демо-ролик не нужны, это ещё 12 МБ.
cp "$TMP/unpacked/models/realesrgan-x4plus.bin" \
   "$TMP/unpacked/models/realesrgan-x4plus.param" "$DEST/models/"

chmod +x "$DEST/realesrgan-ncnn-vulkan"
# Снять карантин, иначе Gatekeeper не даст запустить скачанный бинарник.
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

echo
echo "Готово: $DEST ($(du -sh "$DEST" | cut -f1))"
"$DEST/realesrgan-ncnn-vulkan" -h 2>&1 | head -1 || true
