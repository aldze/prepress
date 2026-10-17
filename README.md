# Prepress Automation

Automated prepress for Adobe InDesign layouts on macOS. It takes a finished
`.indd`, brings every linked image to print standard and saves the result as a
separate `<name>_CMYK.indd`. The source document is never modified.

Full documentation in Russian, with the reasoning and measurements behind each
decision: [README.ru.md](README.ru.md).

## What it does

- **Links to print-ready files.** Converts RGB to CMYK through an ICC profile
  (Relative Colorimetric + BPC), resamples to the target effective resolution,
  crops each image to its frame with a margin, sharpens, and writes JPEG or
  TIFF. It then relinks the layout and keeps the scale and position.
- **Low-resolution images** are upscaled with Real-ESRGAN.
- **Bleed check.** Finds objects that should reach the trim edge but stop
  short of the bleed.
- **Vector links.** Live text in AI/EPS/PDF links is converted to outlines,
  and spot colours to process colours, via Illustrator.
- **Spot channels** in raster files are merged into CMYK via Photoshop.
- **Memory colours** (skin, sky, foliage) and overall tone are measured and
  corrected toward print norms before separation. The correction runs either
  in Photoshop or without Adobe at all (ImageMagick, Apple Vision, Core ML
  human-parsing models). One setting, `engine`, switches between the two.
- **Logs and reports.** Every run writes a log with the exact `magick`
  commands, a JSON before/after manifest and a report in Russian or English.

Three standalone tools sit next to the pipeline:

| Tool | Purpose |
|---|---|
| `export_pdf.py` | Page-by-page export of a finished `_CMYK.indd` to print PDFs (`038_Y123.pdf`) |
| `check_pdf.py` | Preflight of a finished PDF: RGB objects, spot colours, image resolution. Parses the PDF itself, with no Adobe apps |
| `fix_tounicode.py` | Adds missing `/ToUnicode` maps so text copies correctly out of Acrobat. Writes an incremental update; the original bytes stay intact |

## Requirements

- macOS, system `/usr/bin/python3` (3.9, standard library only)
- ImageMagick with lcms
- Adobe InDesign; Illustrator and Photoshop are optional
- Command Line Tools (`swiftc`) for the Adobe-free engine and the macOS app

## Setup

```bash
git clone https://github.com/aldze/prepress.git
cd prepress
./setup.sh        # idempotent; re-run after every git pull
```

`setup.sh` checks for or installs ImageMagick and Real-ESRGAN, builds the
Vision helpers, links the scripts into the InDesign Scripts panel and builds
the `Prepress.app` droplet. The macOS app is built separately with
`./app/build.sh`.

## Usage

```bash
/usr/bin/python3 prepress.py --dry-run path/to/layout.indd   # analysis only, changes nothing
/usr/bin/python3 prepress.py path/to/layout.indd             # real run, writes layout_CMYK.indd
/usr/bin/python3 prepress.py --active                        # the active InDesign document

/usr/bin/python3 export_pdf.py path/to/layout_CMYK.indd      # per-page print PDFs
/usr/bin/python3 check_pdf.py path/to/file.pdf               # preflight
/usr/bin/python3 fix_tounicode.py path/to/file.pdf           # writes file_uni.pdf next to it
```

All settings are in `config.json`; every key has a comment next to it
explaining what it does. `--help` on each script lists the command-line options.

Always start with `--dry-run`. A real run overwrites any existing `_CMYK.indd`.

## Language

Program output (logs, errors, `--help`) is in English. Reports and the macOS
app are available in Russian and English. Code comments and the full
documentation are in Russian.

## License

Source-available, all rights reserved. You may read the code; any other use
needs written permission. See [LICENSE](LICENSE).
