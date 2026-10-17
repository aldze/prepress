#!/bin/bash
# Ставит ВТОРУЮ модель разбора человека — SCHP (LIP), переведённую в Core ML.
#
# Зачем вторая. Модели сильны в разном, и это замерено на кадрах номера.
# MediaPipe держит групповой кадр: на снимке с двенадцатью девушками она
# находит 11.2% кожи, SCHP — 4.3%, потому что LIP учился на кадрах с ОДНИМ
# человеком и мелких людей не видит. Зато на крупной фигуре SCHP заметно
# точнее: на контровом кадре предплечье выходит одним куском (4.0% кадра)
# там, где MediaPipe даёт рваные пятна (2.4%). vskin объединяет результаты,
# поэтому каждая добавляет там, где сильна, и мешать друг другу им нечем.
#
# Почему скриптом, а не файлом в репозитории. Готовая модель весит 127 МБ
# против 8 МБ у MediaPipe — в облачной папке ей не место. Здесь же и перевод:
# нужны torch и coremltools, которых в пайплайне нет и быть не должно,
# поэтому ставятся они во временное окружение и уносятся вместе с ним.
#
# Лицензия: сам SCHP под MIT (Peike Li, 2020). Веса учились на LIP, у датасета
# свои условия, а ONNX взят из стороннего зеркала — для коммерческой печати
# это стоит подтвердить самостоятельно, как и по модели MediaPipe.
set -euo pipefail

DEST="$HOME/Library/Application Support/prepress_automation/tools/models"
MODEL="$DEST/schp_lip.mlpackage"
URL="https://huggingface.co/yisol/IDM-VTON/resolve/main/humanparsing/parsing_lip.onnx"

if [ "${1:-}" = "--check" ]; then
  if [ -d "$MODEL" ]; then echo "SCHP: установлена ($MODEL)"; exit 0; fi
  echo "SCHP: не установлена"; exit 1
fi

if [ -d "$MODEL" ] && [ "${1:-}" != "--force" ]; then
  echo "Уже установлена: $MODEL"
  echo "Переустановить: $0 --force"
  exit 0
fi

command -v /usr/bin/python3 >/dev/null || { echo "Нет /usr/bin/python3"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Скачиваю SCHP LIP (~255 МБ)…"
curl -fL --progress-bar -o "$TMP/parsing_lip.onnx" "$URL"

echo "Ставлю временное окружение для перевода (torch, coremltools)…"
/usr/bin/python3 -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install -q --upgrade pip
# Версии закреплены намеренно: coremltools 7.2 проверен с torch 2.2,
# а onnx тянет за собой protobuf, несовместимый с ним по умолчанию.
"$TMP/venv/bin/pip" install -q "torch==2.2.2" "torchvision==0.17.2" \
    "coremltools==7.2" "numpy<2" "onnx==1.14.1" "protobuf==3.20.3" onnx2torch

cat > "$TMP/convert.py" <<'PYEOF'
# -*- coding: utf-8 -*-
import sys, torch, torch.nn as nn, torch.nn.functional as F
import coremltools as ct
from onnx2torch import convert

SRC, DST = sys.argv[1], sys.argv[2]
SIDE = 473
LABELS = ['Background', 'Hat', 'Hair', 'Glove', 'Sunglasses', 'Upper-clothes', 'Dress',
          'Coat', 'Socks', 'Pants', 'Jumpsuits', 'Scarf', 'Skirt', 'Face', 'Left-arm',
          'Right-arm', 'Left-leg', 'Right-leg', 'Left-shoe', 'Right-shoe']


class Wrap(nn.Module):
    """
    Нормировка и argmax внутри модели.

    SCHP ждёт BGR, нормированный своими mean/std (перевёрнутый порядок
    в оригинале — это и есть BGR). Core ML отдаёт 0..255, поэтому делим
    здесь же: снаружи никто не обязан помнить эти числа.

    Выход — готовый номер класса, а не 20 карт вероятностей: ровно тот же
    контракт, что у модели MediaPipe, и vskin читает его без правок.
    """

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.register_buffer("mean", torch.tensor([0.406, 0.456, 0.485]).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor([0.225, 0.224, 0.229]).view(1, 3, 1, 1))

    def forward(self, x):
        y = (x / 255.0 - self.mean) / self.std
        out = self.core(y)
        # Порядок выходов графа: parsing_result, fusion_result, edge_result.
        # Берём fusion — его же использует simple_extractor.py (output[0][-1]).
        logits = out[1] if isinstance(out, (list, tuple)) else out
        up = F.interpolate(logits, size=(SIDE, SIDE), mode="bilinear", align_corners=False)
        return up.argmax(dim=1).float()


wrap = Wrap(convert(SRC).eval()).eval()
ex = torch.zeros(1, 3, SIDE, SIDE)
traced = torch.jit.trace(wrap, ex, strict=False)
mlmodel = ct.convert(
    traced,
    inputs=[ct.ImageType(name="image", shape=(1, 3, SIDE, SIDE),
                         color_layout=ct.colorlayout.BGR, scale=1.0, bias=[0, 0, 0])],
    outputs=[ct.TensorType(name="labels")],
    convert_to="mlprogram",
    compute_precision=ct.precision.FLOAT16,
    minimum_deployment_target=ct.target.macOS13,
)
mlmodel.user_defined_metadata["labels"] = ",".join(LABELS)
mlmodel.short_description = "SCHP LIP human parsing, 20 classes"
mlmodel.save(DST)
PYEOF

echo "Перевожу в Core ML (пара минут)…"
"$TMP/venv/bin/python" "$TMP/convert.py" "$TMP/parsing_lip.onnx" "$TMP/schp_lip.mlpackage" 2>&1 \
  | grep -viE "warning|tracerwarning|^ *[0-9]+%|has not been tested" || true

[ -d "$TMP/schp_lip.mlpackage" ] || { echo "Перевод не удался"; exit 1; }
mkdir -p "$DEST"
rm -rf "$MODEL"
cp -R "$TMP/schp_lip.mlpackage" "$MODEL"
echo "Готово: $MODEL ($(du -sh "$MODEL" | cut -f1))"
echo "Пайплайн подхватит её сам — обе модели работают вместе."
