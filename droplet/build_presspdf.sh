#!/bin/bash
# Сборка PressPDF.app из PressPDF.applescript — постраничный экспорт в PDF.
# Приложение ставится в ~/Applications, рядом с Prepress.app.
#
# Иконка намеренно другая: два одинаковых значка в Dock — прямой путь бросить
# макет не в то приложение, а Prepress переписывает файлы.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

APP_NAME="PressPDF" \
SRC_FILE="PressPDF.applescript" \
ICNS_FILE="PressPDF.icns" \
BUNDLE_ID="local.presspdf.droplet" \
PY_FILE="export_pdf.py" \
PLACEHOLDER="@@EXPORT_PY@@" \
COPYRIGHT="Постраничный экспорт макетов InDesign в PDF" \
USAGE_DESC="Приложению нужно управлять Adobe InDesign, чтобы открыть вёрстку и экспортировать полосы в PDF." \
  exec bash "$HERE/build_droplet.sh"
