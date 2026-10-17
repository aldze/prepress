#!/bin/bash
# Развёртывание проекта на машине: зависимости, движок ИИ-апскейла,
# скрипты в панель InDesign, приложение-дроплет.
#
# Идемпотентен — можно запускать повторно после git pull.
#
#     ./setup.sh
#
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OK=0
WARN=0
FAIL=0

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
good() { printf '  ✓ %s\n' "$*"; OK=$((OK+1)); }
warn() { printf '  ! %s\n' "$*"; WARN=$((WARN+1)); }
bad()  { printf '  ✗ %s\n' "$*"; FAIL=$((FAIL+1)); }

say "Prepress Automation — установка"
echo "  проект: $HERE"

# --- 1. Система -------------------------------------------------------------
say "1. Система"

if [ "$(uname)" != "Darwin" ]; then
  bad "нужна macOS (обнаружено: $(uname))"
else
  good "macOS $(sw_vers -productVersion)"
fi

if [ -x /usr/bin/python3 ]; then
  good "python3 $(/usr/bin/python3 -V 2>&1 | cut -d' ' -f2) (/usr/bin/python3)"
else
  bad "нет /usr/bin/python3 — поставьте Command Line Tools: xcode-select --install"
fi

# --- 2. ImageMagick ---------------------------------------------------------
say "2. ImageMagick"

MAGICK=""
for c in /opt/homebrew/bin/magick /usr/local/bin/magick "$(command -v magick 2>/dev/null)"; do
  [ -n "$c" ] && [ -x "$c" ] && MAGICK="$c" && break
done

if [ -z "$MAGICK" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "  не найден, ставлю через Homebrew…"
    brew install imagemagick && MAGICK="$(command -v magick)"
  else
    bad "нет ImageMagick и нет Homebrew. Поставьте brew (https://brew.sh), затем: brew install imagemagick"
  fi
fi

if [ -n "$MAGICK" ]; then
  good "$("$MAGICK" -version | head -1 | cut -c1-60)"
  # lcms обязателен: без него не будет цветоделения по ICC
  if "$MAGICK" -version | grep -q lcms; then
    good "поддержка ICC (lcms) есть"
  else
    bad "ImageMagick собран без lcms — цветоделение работать не будет"
  fi
fi

# --- 3. ICC-профиль ---------------------------------------------------------
say "3. ICC-профиль типографии"

# Разрешаем путь той же логикой, что и сам prepress.py: относительный от папки
# проекта, ~ , затем поиск по имени в системных папках ColorSync.
ICC_REPORT="$(cd "$HERE" && /usr/bin/python3 -c "
import sys
sys.path.insert(0, '.')
import prepress
cfg = prepress.load_config('config.json')
for key in ('target_icc', 'grayscale_icc', 'assume_rgb_icc', 'assume_gray_icc'):
    val = cfg.get(key)
    if not val:
        continue
    found = prepress.resolve_icc(val)
    print(('OK' if found else 'NO') + '\t' + key + '\t' + str(found or val))
" 2>&1)"

if [ -z "$ICC_REPORT" ] || echo "$ICC_REPORT" | grep -q "Traceback"; then
  bad "не удалось проверить профили (см. config.json)"
else
  while IFS=$'\t' read -r st key path; do
    [ -z "$st" ] && continue
    if [ "$st" = "OK" ]; then
      good "$key: $(basename "$path")"
    else
      warn "$key не найден: $path"
      echo "     Положите профиль в /Library/ColorSync/Profiles/ или укажите"
      echo "     свой путь в config.json"
    fi
  done <<< "$ICC_REPORT"
fi

# --- 4. Движок ИИ-апскейла --------------------------------------------------
say "4. Real-ESRGAN (ИИ-апскейл ниже 200 ppi)"

if bash "$HERE/tools/install_realesrgan.sh" --check >/dev/null 2>&1; then
  good "уже установлен"
else
  echo "  ставлю…"
  if bash "$HERE/tools/install_realesrgan.sh"; then
    good "установлен"
  else
    warn "не установился — ИИ-апскейл будет отключён, остальное работает"
  fi
fi

# --- 5. Illustrator и Photoshop --------------------------------------------
say "5. Illustrator и Photoshop (текст в кривые, плашки в CMYK)"

check_app() {                     # $1 — bundle id, $2 — человеческое имя, $3 — зачем
  local found=""
  for APP in /Applications/*.app /Applications/*/*.app "$HOME/Applications"/*.app; do
    [ -d "$APP" ] || continue
    if [ "$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$APP/Contents/Info.plist" 2>/dev/null)" = "$1" ]; then
      found="$APP"; break
    fi
  done
  if [ -n "$found" ]; then
    good "$2: $(basename "$found")"
  else
    warn "$2 не найден — $3 (проверка и предупреждения работают всегда)"
  fi
}

check_app "com.adobe.illustrator" "Illustrator" \
          "текст в векторных связях не будет переводиться в кривые, а плашки — в CMYK"
check_app "com.adobe.Photoshop" "Photoshop" \
          "плашечные каналы в TIFF/PSD не будут сводиться, такие файлы останутся без обработки"

# --- 6. Скрипты в панель InDesign ------------------------------------------
say "6. Скрипты в панель Scripts"

FOUND_ID=0
for SP in "$HOME/Library/Preferences/Adobe InDesign/"*/*/Scripts/"Scripts Panel"; do
  [ -d "$SP" ] || continue
  FOUND_ID=1
  ln -sfn "$HERE/jsx/Prepress Automation.jsx" "$SP/Prepress Automation.jsx"
  ln -sfn "$HERE/jsx/presspdf_export.jsx" "$SP/PressPDF Export.jsx"
  ln -sfn "$HERE/indesign_scripts/Gutter Compensation.jsx" "$SP/Gutter Compensation.jsx"
  good "$(echo "$SP" | sed "s|$HOME/Library/Preferences/Adobe InDesign/||")"
done
[ "$FOUND_ID" = "0" ] && warn "не нашёл папок InDesign — запустите InDesign хотя бы раз и повторите"

# --- 7. Приложения -----------------------------------------------------------
# Их два и они РАЗНЫЕ: нативное Prepress.app и старый дроплет
# «Prepress droplet.app». Имена обязаны различаться — пока они совпадали,
# каждый прогон setup.sh затирал одно другим.
say "7. Приложение Prepress.app"

if bash "$HERE/app/build.sh" >/dev/null 2>&1; then
  good "собрано: ~/Applications/Prepress.app"
  echo "     При первом запуске macOS спросит разрешение управлять InDesign — согласитесь."
else
  bad "сборка не удалась, подробности: app/build.sh"
fi

say "7a. Дроплет Prepress droplet.app"

if bash "$HERE/droplet/build.sh" >/dev/null 2>&1; then
  good "собран: ~/Applications/Prepress droplet.app"
  echo "     Перетащите его в Dock, чтобы бросать туда .indd."
else
  bad "сборка не удалась, подробности: droplet/build.sh"
fi

# --- 8. Приложение для экспорта PDF -----------------------------------------
say "8. Приложение PressPDF.app"

if bash "$HERE/droplet/build_presspdf.sh" >/dev/null 2>&1; then
  good "собрано: ~/Applications/PressPDF.app"
  echo "     Перетащите его в Dock — туда бросают макеты для постраничного PDF."
  echo "     Иконка намеренно не такая, как у Prepress: перепутать их дорого."
else
  bad "сборка не удалась, подробности: droplet/build_presspdf.sh"
fi

# --- 9. Помощники Apple (вариант обработки без Adobe) -----------------------
say "9. Помощники Apple (вариант без Adobe)"

VISION_DIR="$HOME/Library/Application Support/prepress_automation/tools"
if [ -z "$(command -v swiftc)" ]; then
  warn "swiftc не найден — вариант без Adobe будет недоступен"
  echo "     Ставится вместе с Command Line Tools: xcode-select --install"
else
  mkdir -p "$VISION_DIR"
  # vtone — авто-тон Core Image, vmask — маски людей, лица и ярлыки сюжета
  # из Vision, vskin — маска кожи по разбору человека моделью. Первый нужен
  # при global_tone.engine = coreimage, остальные — при
  # memory_colors.engine = native; все включаются разом словом engine = native.
  for tool in vtone vmask vskin; do
    if swiftc -O "$HERE/tools/vision/$tool.swift" -o "$VISION_DIR/$tool" 2>/dev/null; then
      good "собран: $VISION_DIR/$tool"
    else
      warn "$tool собрать не удалось — вариант без Adobe будет неполным"
    fi
  done

  # Модель разбора человека. Кладём РЯДОМ С ИНСТРУМЕНТАМИ, а не оставляем
  # в репозитории: при первом запуске Core ML компилирует .mlpackage
  # и кладёт .mlmodelc рядом с ним, а мусорить скомпилированным кэшем
  # в рабочей копии git незачем.
  if [ -d "$HERE/tools/vision/models/selfie_multiclass.mlpackage" ]; then
    mkdir -p "$VISION_DIR/models"
    if rsync -a --delete "$HERE/tools/vision/models/selfie_multiclass.mlpackage" \
             "$VISION_DIR/models/" 2>/dev/null; then
      good "модель разбора человека на месте"
    else
      warn "модель разбора человека скопировать не удалось — кожа будет искаться по цвету"
    fi
  else
    warn "модели разбора человека нет в репозитории — кожа будет искаться по цвету"
  fi
  echo "     Нужны, только если в config.json стоит engine = native."
fi

# --- Итог -------------------------------------------------------------------
say "Итог: успешно $OK, предупреждений $WARN, ошибок $FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "Есть ошибки — см. выше."
  exit 1
fi
echo "Проверить работу, ничего не меняя:"
echo "  ./prepress.py --dry-run \"/путь/к/макету.indd\""
echo "  ./export_pdf.py --dry-run \"/путь/к/макету_CMYK.indd\""
