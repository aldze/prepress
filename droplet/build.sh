#!/bin/bash
# Сборка «Prepress droplet.app» из Prepress.applescript.
# Приложение ставится в ~/Applications — путь стабильный, поэтому иконка,
# однажды положенная в Dock, не отвяжется после пересборки.
#
# ИМЯ БУНДЛА НЕ «Prepress»: под этим именем теперь собирается нативное
# приложение (app/build.sh). Пока имя было общим, каждый ./setup.sh молча
# затирал одно другим — приложение «пропадало», а на его месте оказывался
# дроплет, и по виду бундла это было не отличить. Идентификатор бундла
# при этом прежний, так что выданное разрешение управлять InDesign
# переезжает вместе с переименованием.
#
# Вся механика сборки — в build_droplet.sh: она общая с PressPDF.app,
# и держать её в одном месте дешевле, чем чинить одни и те же грабли дважды.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

APP_NAME="Prepress droplet" \
SRC_FILE="Prepress.applescript" \
ICNS_FILE="Prepress.icns" \
BUNDLE_ID="local.prepress.droplet" \
PY_FILE="prepress.py" \
PLACEHOLDER="@@PREPRESS_PY@@" \
COPYRIGHT="Препресс-обработка макетов InDesign" \
USAGE_DESC="Приложению нужно управлять Adobe InDesign, чтобы собрать данные о связях и переподключить их." \
  exec bash "$HERE/build_droplet.sh"
