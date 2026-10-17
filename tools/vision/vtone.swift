//
//  vtone.swift — авто-тон средствами Core Image (macOS, без Adobe).
//
//  Собирается в setup.sh: swiftc -O tools/vision/vtone.swift -o <кэш>/vtone
//  Нужен только при global_tone.engine = "coreimage".
//
//  Apple называет это auto enhance. Под капотом четыре фильтра, и их состав
//  зависит от кадра: CIFaceBalance (баланс по лицам — то же, что у нас делает
//  правка по коже, только встроенная), CIVibrance, CIToneCurve,
//  CIHighlightShadowAdjust. Чисел наружу не отдаёт, поэтому «до» и «после»
//  prepress.py замеряет сам.
//
//  Профиль исходника сохраняется: читаем с ним, пишем с ним же. Работаем
//  в линейном пространстве — так тон-кривая не ломает переходы.
//

import Foundation
import CoreImage
import AppKit

let args = CommandLine.arguments
guard args.count >= 3 else { print("vtone <input> <output.tif> [noface]"); exit(2) }
let inURL = URL(fileURLWithPath: args[1])
let outURL = URL(fileURLWithPath: args[2])
let useFaces = !(args.count > 3 && args[3] == "noface")

guard var image = CIImage(contentsOf: inURL) else { print("could not read the input"); exit(2) }
let srcSpace = image.colorSpace ?? CGColorSpace(name: CGColorSpace.sRGB)!

var opts: [CIImageAutoAdjustmentOption: Any] = [
    .enhance: true,
    .redEye: false
]
if !useFaces { opts[.features] = [] }

let filters = image.autoAdjustmentFilters(options: opts)
var applied: [String] = []
let t0 = Date()
for f in filters {
    f.setValue(image, forKey: kCIInputImageKey)
    if let out = f.outputImage {
        image = out
        applied.append(f.name)
    }
}
let ctx = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!])
do {
    try ctx.writeTIFFRepresentation(of: image, to: outURL, format: .RGBA8, colorSpace: srcSpace)
} catch {
    print("could not write the output: \(error)"); exit(3)
}
print(String(format: "filters: %d (%@) in %.2f s",
             applied.count, applied.joined(separator: ", "), -t0.timeIntervalSinceNow))
