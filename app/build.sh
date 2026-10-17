#!/bin/bash
# Сборка Prepress.app без Xcode.
#
# На машине стоят только Command Line Tools, значит xcodebuild недоступен:
# компилируем swiftc и собираем бандл руками — ровно как droplet/build.sh
# делает с osacompile. Проверено: SwiftUI из SDK 15.2 так собирается
# и запускается.
#
# Правила, унаследованные от сборки дроплета, — каждое из пойманного бага:
#   * бандл удаляется ПЕРЕД сборкой, а не пишется поверх: иначе Info.plist
#     сливается со старым и копятся ресурсы прежних сборок;
#   * подпись ставится ПОСЛЕДНЕЙ: правка Info.plist после подписи её ломает,
#     и приложение теряет право управлять InDesign;
#   * пути к пайплайну — плейсхолдеры, конкретного пути в исходнике нет.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
APP="${1:-$HOME/Applications/Prepress.app}"
NAME="Prepress"
ID="local.prepress.app"
MIN_MACOS="13.0"

say()  { printf "\033[1m%s\033[0m\n" "$*"; }
good() { printf "  ✓ %s\n" "$*"; }
die()  { printf "  ✗ %s\n" "$*" >&2; exit 1; }

say "Сборка $NAME"

command -v swiftc >/dev/null || die "swiftc не найден — xcode-select --install"
SDK="$(xcrun --show-sdk-path)"
[ -d "$SDK" ] || die "SDK не найден"
good "SDK $(xcrun --show-sdk-version)"

# --- исходники с подстановкой путей ------------------------------------------
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
mkdir -p "$BUILD/src"
while IFS= read -r -d '' f; do
  rel="${f#$HERE/Sources/}"
  mkdir -p "$BUILD/src/$(dirname "$rel")"
  sed "s|@@REPO_ROOT@@|$REPO|g" "$f" > "$BUILD/src/$rel"
done < <(find "$HERE/Sources" -name '*.swift' -print0)

# --- компиляция ---------------------------------------------------------------
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Собираем ВСЕ файлы одним вызовом: swiftc в режиме одного модуля должен
# видеть их разом, иначе типы из соседних файлов ему неизвестны.
# mapfile — это bash 4, а в macOS до сих пор bash 3.2: собираем массив
# переносимо, через while read.
SRCS=()
while IFS= read -r f; do SRCS+=("$f"); done < <(find "$BUILD/src" -name '*.swift' | sort)
swiftc -O -parse-as-library \
       -target arm64-apple-macos$MIN_MACOS \
       -sdk "$SDK" \
       -o "$APP/Contents/MacOS/$NAME" \
       "${SRCS[@]}" || die "компиляция не прошла"
good "скомпилировано: ${#SRCS[@]} файлов"

# --- иконка -------------------------------------------------------------------
# Только CFBundleIconFile, без CFBundleIconName и Assets.car: найдя первый
# ключ, macOS ищет иконку в каталоге ассетов и игнорирует .icns (тот же
# урок, что у дроплета). Исходник — Resources/icon-1024.png (плашка уже
# вырезана, углы прозрачные); .icns собирается из него iconset'ом
# 16…512 @1x/@2x и `iconutil -c icns`.
cp "$HERE/Resources/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns" \
  || die "нет Resources/AppIcon.icns"
good "иконка"

# --- Info.plist ---------------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleExecutable</key><string>$NAME</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIdentifier</key><string>$ID</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.graphics-design</string>
  <!-- Приложение управляет Adobe по Apple Events: без этих строк macOS
       откажет молча, и прогон упадёт на первом же обращении к InDesign. -->
  <key>NSAppleEventsUsageDescription</key>
  <string>Prepress управляет InDesign, Photoshop и Illustrator, чтобы разобрать вёрстку и обработать связанные изображения.</string>
  <key>NSDesktopFolderUsageDescription</key>
  <string>Чтобы обработать вёрстку и изображения, лежащие на рабочем столе.</string>
  <key>NSDocumentsFolderUsageDescription</key>
  <string>Чтобы обработать вёрстку и изображения из папки «Документы».</string>
  <key>NSDownloadsFolderUsageDescription</key>
  <string>Чтобы обработать вёрстку и изображения из папки «Загрузки».</string>
  <key>NSRemovableVolumesUsageDescription</key>
  <string>Номера часто лежат на внешнем диске.</string>
  <key>CFBundleDocumentTypes</key>
  <array><dict>
    <key>CFBundleTypeName</key><string>InDesign layout</string>
    <key>CFBundleTypeRole</key><string>Viewer</string>
    <key>LSItemContentTypes</key><array><string>com.adobe.indesign-document</string></array>
  </dict></array>
</dict></plist>
PLIST
good "Info.plist"

# --- подпись (последней!) -----------------------------------------------------
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 \
  && good "подписано ad-hoc" \
  || die "подписать не удалось"

say "Готово: $APP"
echo "  Пайплайн берётся из $REPO (отладочная сборка)."
echo "  Запустить: open '$APP'"
