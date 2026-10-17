#!/bin/bash
# Сборка апплета-дроплета из .applescript. Общая часть для всех приложений
# проекта: build.sh собирает этим Prepress.app, build_presspdf.sh — PressPDF.app.
#
# Здесь собраны все грабли, на которые проект уже наступил (см. комментарии
# по тексту). Поэтому нового сборочного скрипта заводить не надо — надо
# добавить ещё один тонкий вызов этого.
#
# Переменные окружения, которые обязан задать вызывающий:
#   APP_NAME     Prepress              имя приложения и бандла
#   SRC_FILE     Prepress.applescript  исходник рядом с этим скриптом
#   ICNS_FILE    Prepress.icns         иконка (необязательно)
#   BUNDLE_ID    local.prepress.droplet
#   PY_FILE      prepress.py           скрипт в корне проекта
#   PLACEHOLDER  @@PREPRESS_PY@@       что заменить на путь к PY_FILE
#   USAGE_DESC   текст системного запроса на управление другими приложениями
#   COPYRIGHT    строка NSHumanReadableCopyright
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/${APP_NAME}.app"
SRC="$HERE/$SRC_FILE"
ICNS="$HERE/${ICNS_FILE:-}"

PROJECT="$(cd "$HERE/.." && pwd)"
PY_PATH="$PROJECT/$PY_FILE"
if [ ! -f "$PY_PATH" ]; then
  echo "Не найден $PY_PATH" >&2
  exit 1
fi
if [ ! -f "$SRC" ]; then
  echo "Не найден $SRC" >&2
  exit 1
fi

echo "Сборка $APP"
echo "  $PY_FILE: $PY_PATH"
mkdir -p "$HOME/Applications"

# Путь к python-скрипту подставляем в исходник на лету: в репозитории его нет,
# поэтому проект переносится на любую машину без правки кода.
TMP_SRC="$(mktemp -t prepress_droplet).applescript"
trap 'rm -f "$TMP_SRC"' EXIT
/usr/bin/sed "s|${PLACEHOLDER}|${PY_PATH//|/\\|}|" "$SRC" > "$TMP_SRC"

# ОБЯЗАТЕЛЬНО собирать с нуля. osacompile -o пишет в существующий бандл, не
# очищая его: файлы прежних сборок остаются, а Info.plist сливается со старым
# вместо перезаписи из шаблона. Так приложение постепенно превращается
# в гибрид сборок разных дней — теряются Assets.car и CFBundleIconName,
# копятся неиспользуемые ресурсы. Симптом со стороны человека: окну
# перестают доставляться события ввода, оно 10-15 секунд не реагирует
# на клики и крутит «мяч». Переписывание скрипта от этого не спасает —
# портится бандл, а не код.
# Замер: накопленный бандл — 14.35 с задержки HID на клик; чистый — 0.
case "$APP" in
  "$HOME/Applications/"*.app) rm -rf "$APP" ;;
  *) echo "Отказ: $APP не похож на цель сборки" >&2; exit 1 ;;
esac

osacompile -o "$APP" "$TMP_SRC"

PLIST="$APP/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy

# Иконка. Имя файла берём из самого бандла: у скриптов-дроплетов это
# droplet.icns, у обычных апплетов — applet.icns.
if [ -n "${ICNS_FILE:-}" ] && [ -f "$ICNS" ]; then
  ICON_NAME="$($PB -c "Print :CFBundleIconFile" "$PLIST" 2>/dev/null || echo applet)"
  ICON_NAME="${ICON_NAME%.icns}"
  cp "$ICNS" "$APP/Contents/Resources/${ICON_NAME}.icns"
  echo "иконка -> Resources/${ICON_NAME}.icns"

  # macOS предпочитает CFBundleIconName и ищет иконку по этому имени в
  # Assets.car — а там лежит стоковая иконка osacompile, не наша. Итог:
  # приложение остаётся без иконки, хотя .icns на месте. Убираем и ключ,
  # и сам каталог, чтобы система вернулась к CFBundleIconFile.
  #
  # Раньше это не мешало по случайности: бандл пересобирался поверх старого,
  # в котором ни CFBundleIconName, ни Assets.car не было вовсе. Как только
  # сборка пошла с нуля (см. выше), osacompile завёл их заново — и иконка
  # пропала.
  $PB -c "Delete :CFBundleIconName" "$PLIST" 2>/dev/null || true
  rm -f "$APP/Contents/Resources/Assets.car"
fi

set_plist() {  # ключ тип значение
  $PB -c "Delete :$1" "$PLIST" 2>/dev/null || true
  $PB -c "Add :$1 $2 $3" "$PLIST"
}

set_plist CFBundleName string "$APP_NAME"
set_plist CFBundleDisplayName string "$APP_NAME"
set_plist CFBundleIdentifier string "$BUNDLE_ID"
set_plist CFBundleShortVersionString string "1.0"
set_plist CFBundleVersion string "1"
set_plist NSHumanReadableCopyright string "$COPYRIGHT"
# Текст системного запроса на управление другими приложениями
set_plist NSAppleEventsUsageDescription string "$USAGE_DESC"

# Принимаем .indd перетаскиванием. LSHandlerRank=None — чтобы приложение
# не набивалось в обработчики .indd по умолчанию вместо самого InDesign.
$PB -c "Delete :CFBundleDocumentTypes" "$PLIST" 2>/dev/null || true
$PB -c "Add :CFBundleDocumentTypes array" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0 dict" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeName string 'InDesign Document'" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Viewer" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:LSHandlerRank string None" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions array" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:0:CFBundleTypeExtensions:0 string indd" "$PLIST"
# Папки тоже можно бросать — внутри ищутся .indd
$PB -c "Add :CFBundleDocumentTypes:1 dict" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:1:CFBundleTypeName string Folder" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:1:CFBundleTypeRole string Viewer" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:1:LSHandlerRank string None" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:1:LSItemContentTypes array" "$PLIST"
$PB -c "Add :CFBundleDocumentTypes:1:LSItemContentTypes:0 string public.folder" "$PLIST"

# ВАЖНО: подписать заново.
# osacompile подписывает бандл сразу, а мы после этого правили Info.plist и
# иконку — подпись становится невалидной ("plist or signature have been
# modified"). С битой подписью macOS не выдаёт приложению права на Apple Events,
# и любой вызов InDesign падает с ошибкой -1743.
codesign --force --deep --sign - --identifier "$BUNDLE_ID" "$APP"
codesign --verify --strict "$APP" && echo "подпись: OK ($BUNDLE_ID)"

# Разрешение на управление InDesign macOS помнит по идентификатору бандла,
# а он у нас стабильный. Раньше здесь стоял `tccutil reset AppleEvents`,
# из-за чего система требовала подтверждения после КАЖДОЙ пересборки —
# без всякой нужды. Сбросить вручную, если когда-нибудь понадобится:
#     tccutil reset AppleEvents <BUNDLE_ID>

# Сбросить кеши Finder/LaunchServices, иначе иконка обновится не сразу
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true

echo "Готово: $APP"
