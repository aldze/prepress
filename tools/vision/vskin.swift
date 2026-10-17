// Маска КОЖИ по разбору человека: где кожа, а где одежда.
//
// Зачем понадобилось. Цвет отделить кожу от ткани телесного оттенка не может
// в принципе: на групповом кадре «кожей» становилось 15% кадра вместе с белыми
// кофтами и бежевыми брюками, и никакой порог этого не лечит. Модель разбора
// человека размечает кожу тела и кожу лица ОТДЕЛЬНО от одежды и волос —
// знание, а не догадка по цвету.
//
// Что выяснено замерами, и без чего модель работает плохо:
//
//   1. РАЗРЕШЕНИЕ ИСХОДНИКА НИ ПРИ ЧЁМ, важен масштаб человека ВНУТРИ входа.
//      Вход модели прибит гвоздями (256x256): одна и та же коробка вокруг
//      лица, поданная в 500 и в 1000 пикселей, дала 7.1% и 7.1% — совпадение
//      до десятой доли. Подавать оригинал до даунскейла бессмысленно.
//
//   2. ГЛАВНЫЙ ПРОХОД — ПЛИТКИ, а не вырезка по фигуре. Кадр режется на
//      перекрывающиеся квадраты в половину большей стороны: люди внутри
//      входа становятся вчетверо крупнее, а картинка остаётся настоящей.
//      Замер на групповом кадре: ноги пятерых девушек вырезкой по фигуре
//      находились огрызками (4.8% кадра), плитками — целиком (8.9%).
//
//   3. ФОН В ВЫРЕЗКЕ ГАСИТЬ НЕЛЬЗЯ. Казалось разумным залить всё, кроме
//      фигуры, серым, чтобы модель не бралась за соседа. На деле человек
//      на плоской заливке — это то, чего в обучении не было: покрытие
//      падало на всех проверенных кадрах (4.76 против 5.76, 3.22 против
//      5.78, 6.35 против 6.91, 3.02 против 4.08). Фон остаётся как есть.
//
//   4. ЦЕЛЫЙ КАДР ОДНИМ ПРОХОДОМ — только на крайний случай. Люди в нём
//      мелкие, и на кадре с балеринами модель записала в кожу белые пачки
//      целиком (6.1% против 4.8% кропами, и лишнее — это юбки). Плитки
//      дают то же покрытие без этой ошибки. Дополнять целый кадр до 3:4,
//      как фигуру, бесполезно: 6.32% против 6.13%, то есть ничего.
//
//   5. ПРОПОРЦИИ ВЫРЕЗКИ почти не решают. Дополнение фигуры до 3:4, до
//      квадрата и до 4:3 дало 4.76 / 5.06 / 4.17 — в пределах разброса.
//      Константа 0.75 досталась от другой модели; проверена и оставлена.
//
// Поэтому проходов три, а результат — их ОБЪЕДИНЕНИЕ: плитки (основа),
// каждое лицо (мелкие лица и детали вроде лба над очками) и каждая фигура
// (добавляет немного там, где Vision уверенно нашёл человека). Каждый
// проход стоит один прогон 256x256, весь разбор — около полутора секунд.
//
// Ответ — PNG с маской кожи по всему кадру плюс строка JSON в stdout.
import Foundation
import Vision
import CoreML
import CoreImage
import Accelerate

var SIDE = 256              // размер входа берётся из модели
let BODY_PAD: CGFloat = 0.12    // воздух вокруг фигуры
let FACE_PAD: CGFloat = 1.15    // вокруг лица берём втрое больше самого лица
// Пропорции, до которых дополняется вырезка. Для ФИГУРЫ это 3:4 портрета:
// стоящий человек имеет отношение сторон около 0.3, и дополнение до 0.75
// с последующим растяжением во входной квадрат оставляет его узнаваемым.
// Для ЦЕЛОГО КАДРА так делать НЕЛЬЗЯ: кадр 6720x4480 — это 1.5, дополнение
// до 0.75 добавляет вдвое больше пустого поля сверху и снизу, чем есть самого
// кадра, люди сжимаются вчетверо, и модель начала записывать в кожу пачки
// балерин (маска 6.3% против 3.4% и белая юбка целиком внутри). Целый кадр
// дополняется до КВАДРАТА, то есть не искажается вовсе.
var BODY_ASPECT: CGFloat = 0.75
var FRAME_ASPECT: CGFloat = 1.0

func fail(_ why: String) -> Never {
    let e = why.replacingOccurrences(of: "\"", with: "'")
    print("{\"ok\":false,\"error\":\"\(e)\"}"); exit(1)
}

var args = CommandLine.arguments
// Какие проходы делать. Нужно не для настройки, а для РАЗБОРА: когда в маске
// появляется лишнее, единственный способ найти виноватого — прогнать проходы
// поодиночке, а не рассуждать, который из них мог бы.
var doWhole = true, doBodies = true, doFaces = true, doTiles = true
if let i = args.firstIndex(where: { $0.hasPrefix("--passes=") }) {
    let want = Set(args[i].dropFirst(9).split(separator: ",").map(String.init))
    doWhole = want.contains("whole"); doBodies = want.contains("bodies")
    doFaces = want.contains("faces"); doTiles = want.contains("tiles")
    args.remove(at: i)
}
// Пропорции вырезки — тоже для разбора: константа 0.75 досталась от другой
// модели, и проверять её надо замером, а не верой.
// Гашение фона в вырезке по фигуре: по умолчанию ВЫКЛЮЧЕНО, см. пункт 3
// в шапке. Флаг оставлен, чтобы замер можно было повторить.
var greyOut = false
if let i = args.firstIndex(where: { $0 == "--grey-background" }) {
    greyOut = true; args.remove(at: i)
}
var TILE_DIV = 0.0        // 0 = подбирать по размеру лиц
var TILE_STEP = 0.5       // доля стороны плитки: меньше — больше перекрытие
if let i = args.firstIndex(where: { $0.hasPrefix("--tile-step=") }) {
    TILE_STEP = max(0.2, min(1.0, Double(args[i].dropFirst("--tile-step=".count)) ?? 0.5))
    args.remove(at: i)
}
if let i = args.firstIndex(where: { $0.hasPrefix("--tile-div=") }) {
    TILE_DIV = max(0.0, Double(args[i].dropFirst("--tile-div=".count)) ?? 0.0)
    args.remove(at: i)
}
// Порог ярлыка «люди» у классификатора сюжета; 0 — ворота выключены.
var PEOPLE_MIN = 0.3
if let i = args.firstIndex(where: { $0.hasPrefix("--people-min=") }) {
    PEOPLE_MIN = max(0.0, Double(args[i].dropFirst("--people-min=".count)) ?? 0.3)
    args.remove(at: i)
}
if let i = args.firstIndex(where: { $0.hasPrefix("--body-aspect=") }) {
    BODY_ASPECT = CGFloat(Double(args[i].dropFirst(14)) ?? 0.75)
    args.remove(at: i)
}
guard args.count >= 4 else {
    fail("vskin [--passes=tiles,bodies,faces,whole] [--tile-div=N] [--people-min=0.3] [--body-aspect=…] [--grey-background] <вход> <папка> <модель.mlpackage> [ещё модели…]")
}
let inPath = args[1], outDir = args[2]
let modelPaths = Array(args[3...])

guard let src = CIImage(contentsOf: URL(fileURLWithPath: inPath)) else { fail("файл не прочитан") }
let W = Int(src.extent.width), H = Int(src.extent.height)
let FULL = CGRect(x: 0, y: 0, width: CGFloat(W), height: CGFloat(H))
let ctx = CIContext()
let t0 = Date()

// Классы кожи по порядковому номеру. У разных моделей нумерация своя,
// поэтому список имён приходит в метаданных; здесь — уже номера.
func skinIndices(_ model: MLModel) -> Set<Int> {
    let names = (model.modelDescription.metadata[.creatorDefinedKey] as? [String: String])?["labels"]
    guard let list = names?.split(separator: ",").map({ String($0).lowercased() }) else {
        return [11, 12, 13, 14, 15]          // разметка ATR по умолчанию
    }
    // Имена классов кожи у разных моделей разные: у разбора тела это части
    // (left-arm, right-leg…), у MediaPipe — body-skin и face-skin.
    let want: Set<String> = ["face", "left-arm", "right-arm", "left-leg", "right-leg",
                             "neck", "torso-skin", "body-skin", "face-skin", "skin"]
    var out = Set<Int>()
    for (i, n) in list.enumerated() where want.contains(n) { out.insert(i) }
    return out.isEmpty ? [11, 12, 13, 14, 15] : out
}

/// Классы, которые кожей быть НЕ МОГУТ, хотя лежат ровно там, где её ждёшь:
/// перчатка на кисти, носок на щиколотке, ботинок на стопе. У разбора тела
/// они размечены отдельно, и это знание дороже догадки: на кадре гольфиста
/// проход по целому кадру обрывал маску ровно по краю перчатки, а плитки
/// тот же кусок записывали в руку. Что хоть один проход назвал перчаткой,
/// из кожи вычитается.
func vetoIndices(_ model: MLModel) -> Set<Int> {
    let names = (model.modelDescription.metadata[.creatorDefinedKey] as? [String: String])?["labels"]
    guard let list = names?.split(separator: ",").map({ String($0).lowercased() }) else { return [] }
    let want: Set<String> = ["glove", "socks", "sock", "left-shoe", "right-shoe", "shoe"]
    var out = Set<Int>()
    for (i, n) in list.enumerated() where want.contains(n) { out.insert(i) }
    return out
}

/// Готовая к загрузке модель. `.mlpackage` компилируется на первом запуске
/// и кладётся рядом: `coremlcompiler` живёт только в полном Xcode, а у нас
/// Command Line Tools, зато компиляция есть в самом Core ML.
func compiled(_ path: String) -> URL? {
    let u = URL(fileURLWithPath: path)
    if path.hasSuffix(".mlmodelc") { return u }
    let cache = u.deletingPathExtension().appendingPathExtension("mlmodelc")
    if FileManager.default.fileExists(atPath: cache.path) { return cache }
    guard let tmp = try? MLModel.compileModel(at: u) else { return nil }
    try? FileManager.default.removeItem(at: cache)
    try? FileManager.default.moveItem(at: tmp, to: cache)
    return FileManager.default.fileExists(atPath: cache.path) ? cache : tmp
}

// --- что Vision нашёл в кадре ----------------------------------------------
let handler = VNImageRequestHandler(url: URL(fileURLWithPath: inPath), options: [:])

var personMasks: [CIImage] = []
let inst = VNGeneratePersonInstanceMaskRequest()
do {
    try handler.perform([inst])
    if let o = inst.results?.first {
        for one in o.allInstances {
            if let buf = try? o.generateScaledMaskForImage(forInstances: [one], from: handler) {
                personMasks.append(CIImage(cvPixelBuffer: buf))
            }
        }
    }
} catch { /* людей нет */ }

// Лица ищем ОТДЕЛЬНО от фигур: на кадре с корветом разбор по людям нашёл
// одного человека из двух, а лица — оба. Эти два запроса промахиваются
// в разных местах, поэтому берутся оба.
var faceBoxes: [CGRect] = []
let faceReq = VNDetectFaceRectanglesRequest()
do {
    try handler.perform([faceReq])
    for f in (faceReq.results ?? []) {
        // boundingBox нормализован и считается снизу вверх — как CIImage.
        let b = f.boundingBox
        faceBoxes.append(CGRect(x: b.minX * CGFloat(W), y: b.minY * CGFloat(H),
                                width: b.width * CGFloat(W), height: b.height * CGFloat(H)))
    }
} catch { /* лиц нет */ }

// --- есть ли в кадре люди вообще -------------------------------------------
// Ворота ДО загрузки моделей. Без них плитки режутся по любому кадру,
// и модель находит «кожу» там, где людей нет: на интерьере SCHP
// записал в кожу 9.7% кадра — оранжевую колонну и пятно на потолке, —
// и по «яркости кожи» колонны кадр вытягивался как недодержанный. Заодно
// уходят 8–16 секунд на кадр: на номере с интерьерами люди были на одном кадре из 20.
//
// Одних детекторов мало: на многолюдном кадре они слепнут разом (снимок
// с шестьюдесятью детьми — ноль лиц и ноль фигур). Поэтому при пустых
// детекторах спрашивается классификатор сюжета: он смотрит на кадр целиком
// и людей видит и там. Замер: на номере все пять кадров с людьми, где
// детекторы не нашли никого, дали people 0.74…0.94; кадры без людей — 0.00;
// интерьеры другого номера — не выше 0.14. Порог 0.3 стоит в широком разрыве.
var peopleLabel = -1.0
if faceBoxes.isEmpty && personMasks.isEmpty && PEOPLE_MIN > 0 {
    let cls = VNClassifyImageRequest()
    try? handler.perform([cls])
    let wanted: Set<String> = ["people", "adult", "child", "crowd", "teen", "baby", "portrait"]
    peopleLabel = Double((cls.results ?? []).filter { wanted.contains($0.identifier) }
                          .map { $0.confidence }.max() ?? 0)
    if peopleLabel < PEOPLE_MIN {
        print("{\"ok\":true,\"people\":false,"
            + "\"peopleLabel\":\(String(format: "%.2f", peopleLabel)),"
            + "\"whole\":0,\"bodies\":0,\"faces\":0,\"tiles\":0,\"models\":[],"
            + "\"share\":0,\"seconds\":\(String(format: "%.2f", -t0.timeIntervalSinceNow))}")
        exit(0)
    }
}

// --- модели разбора: грузятся только когда людям есть место --------------
var models: [(MLModel, Set<Int>, Set<Int>, Int)] = []
for p in modelPaths {
    guard let u = compiled(p), let m = try? MLModel(contentsOf: u) else {
        fail("модель не загрузилась: \(p)")
    }
    // Размер входа У КАЖДОЙ МОДЕЛИ СВОЙ: у MediaPipe 256, у SCHP 473.
    // Общего SIDE тут быть не может — пока он был один, вторая модель
    // перетирала его собой, первая получала чужой буфер, и объединение
    // двух моделей давало МЕНЬШЕ, чем каждая по отдельности (4.28% против
    // 11.23% на групповом кадре). Заметить это можно только числом:
    // маска не ломалась, а тихо беднела.
    var side = 256
    if let d = m.modelDescription.inputDescriptionsByName["image"],
       let c = d.imageConstraint {
        side = Int(c.pixelsWide)
    }
    models.append((m, skinIndices(m), vetoIndices(m), side))
}

// --- накопитель маски по всему кадру ---------------------------------------
var acc = [UInt8](repeating: 0, count: W * H)
// Накопитель НА КАЖДУЮ модель отдельно. Нужен не ради отчёта: по расхождению
// моделей видно, можно ли маске верить вообще. Там, где они согласны, маска
// надёжна; где одна даёт втрое больше другой — это заливка, а не находка.
// Замер: гольфист 2.4% и 4.0% (модели дополняют друг друга), групповой кадр
// 11.2% и 4.0%, и на нём в «кожу» попали юбки, грамоты и тёмный фон.
var accEach = [[UInt8]]()
// Вето: перчатки, носки, обувь. Вычитается из кожи в самом конце,
// когда все проходы отработали, — иначе порядок проходов решал бы,
// что победит.
var nope = [UInt8](repeating: 0, count: W * H)

func maskBounds(_ m: CIImage) -> CGRect? {
    // Границы фигуры: считаем по уменьшенной копии, точности хватает.
    let s = 256.0 / max(m.extent.width, m.extent.height)
    let small = m.transformed(by: CGAffineTransform(scaleX: s, y: s))
    let w = Int(small.extent.width), h = Int(small.extent.height)
    var px = [UInt8](repeating: 0, count: w * h)
    ctx.render(small, toBitmap: &px, rowBytes: w, bounds: small.extent,
               format: .L8, colorSpace: CGColorSpaceCreateDeviceGray())
    var x0 = w, y0 = h, x1 = -1, y1 = -1
    for y in 0..<h { for x in 0..<w where px[y*w + x] > 127 {
        if x < x0 { x0 = x }; if x > x1 { x1 = x }
        if y < y0 { y0 = y }; if y > y1 { y1 = y }
    } }
    if x1 < 0 { return nil }
    let k = 1.0 / s
    return CGRect(x: CGFloat(x0)*k, y: CGFloat(y0)*k,
                  width: CGFloat(x1-x0+1)*k, height: CGFloat(y1-y0+1)*k)
}

let grey = CIImage(color: CIColor(red: 0.5, green: 0.5, blue: 0.5))

/// Один проход: вырезать коробку, дополнить серым до пропорций модели,
/// разобрать и подмешать найденное в общий накопитель.
///
/// `only` — маска фигуры: фон гасим серым, чтобы модель не бралась за соседа.
/// Для лица и целого кадра гасить нечего, туда приходит nil.
func pass(_ box0: CGRect, only: CIImage?, aspect: CGFloat = BODY_ASPECT) -> Bool {
    let box = box0.intersection(FULL)
    if box.width < 32 || box.height < 32 { return false }

    var image = src
    if let m = only {
        guard let blend = CIFilter(name: "CIBlendWithMask", parameters: [
            kCIInputImageKey: src, kCIInputBackgroundImageKey: grey.cropped(to: FULL),
            kCIInputMaskImageKey: m
        ])?.outputImage else { return false }
        image = blend
    }
    let crop = image.cropped(to: box)

    // дополняем до пропорций модели, НЕ растягивая
    var tw = box.width, th = box.height
    if tw < th * aspect { tw = th * aspect } else { th = tw / aspect }
    let offX = (tw - box.width) / 2, offY = (th - box.height) / 2
    let padded = crop
        .transformed(by: CGAffineTransform(translationX: -box.minX + offX, y: -box.minY + offY))
        .composited(over: grey.cropped(to: CGRect(x: 0, y: 0, width: tw, height: th)))
    let x0 = max(0, Int(box.minX)), x1 = min(W, Int(box.maxX))
    let y0 = max(0, Int(box.minY)), y1 = min(H, Int(box.maxY))

    // --- разбор каждой моделью, объединяем в кадре --------------------------
    for (modelIndex, entry) in models.enumerated() {
        let (model, want, veto, SIDE) = entry
        let scaled = padded.transformed(by: CGAffineTransform(scaleX: CGFloat(SIDE)/tw,
                                                              y: CGFloat(SIDE)/th))
        var pb: CVPixelBuffer?
        CVPixelBufferCreate(nil, SIDE, SIDE, kCVPixelFormatType_32BGRA,
                            [kCVPixelBufferCGImageCompatibilityKey: true] as CFDictionary, &pb)
        guard let buf = pb else { continue }
        ctx.render(scaled, to: buf, bounds: CGRect(x: 0, y: 0, width: SIDE, height: SIDE),
                   colorSpace: CGColorSpaceCreateDeviceRGB())

        var skin = [Bool](repeating: false, count: SIDE * SIDE)
        var nay = [Bool](repeating: false, count: SIDE * SIDE)
        let slot = modelIndex
        guard let prov = try? MLDictionaryFeatureProvider(dictionary: ["image": MLFeatureValue(pixelBuffer: buf)]),
              let out = try? model.prediction(from: prov),
              let arr = out.featureValue(for: "labels")?.multiArrayValue else { continue }
        // Тип выхода читаем ИЗ МОДЕЛИ, а не по памяти: конвертер отдаёт
        // Float16, и чтение его как Float32 роняет процесс по SIGSEGV молча,
        // без единой строки в stderr.
        //
        // ШАГИ тоже берём из массива, а не считаем по ширине. Core ML
        // выравнивает строку, и у модели шириной 473 в строке лежит БОЛЬШЕ
        // 473 элементов; линейное чтение съезжает на добавку каждую строку,
        // и маска выходит диагональными полосами через весь кадр. На 256
        // ошибки не было видно: 256 уже кратно выравниванию, добавки нет.
        let dims = arr.shape.map { $0.intValue }
        let steps = arr.strides.map { $0.intValue }
        let rows = dims.count >= 2 ? dims[dims.count - 2] : SIDE
        let cols = dims.count >= 1 ? dims[dims.count - 1] : SIDE
        let rowStep = steps.count >= 2 ? steps[steps.count - 2] : cols
        let colStep = steps.count >= 1 ? steps[steps.count - 1] : 1
        let rowsN = min(rows, SIDE), colsN = min(cols, SIDE)
        let cap = max(arr.count, rows * rowStep)

        func mark(_ value: (Int) -> Float) {
            for y in 0..<rowsN {
                let base = y * rowStep
                for x in 0..<colsN {
                    let k = Int(value(base + x * colStep).rounded())
                    if want.contains(k) { skin[y * SIDE + x] = true }
                    else if veto.contains(k) { nay[y * SIDE + x] = true }
                }
            }
        }

        switch arr.dataType {
        case .float16:
            let ptr = arr.dataPointer.bindMemory(to: UInt16.self, capacity: cap)
            mark { Float(Float16(bitPattern: ptr[$0])) }
        case .float32:
            let ptr = arr.dataPointer.bindMemory(to: Float32.self, capacity: cap)
            mark { Float(ptr[$0]) }
        case .double:
            let ptr = arr.dataPointer.bindMemory(to: Double.self, capacity: cap)
            mark { Float(ptr[$0]) }
        case .int32:
            let ptr = arr.dataPointer.bindMemory(to: Int32.self, capacity: cap)
            mark { Float(ptr[$0]) }
        @unknown default:
            fail("неизвестный тип выхода модели: \(arr.dataType.rawValue)")
        }

        // --- обратно в координаты кадра -------------------------------------
        //
        // Идём по пикселям КАДРА и берём значение из маски SIDE x SIDE, а не
        // наоборот. Обратный порядок оставляет дыры: область человека больше
        // входа модели, и точки ложатся врассыпную — маска выходила 0.09%
        // вместо 5%.
        for fy in y0..<y1 {
            let iy = H - 1 - fy              // CoreImage снизу вверх, растр — сверху вниз
            if iy < 0 || iy >= H { continue }
            // Модель отдаёт растр СВЕРХУ ВНИЗ, а координаты кадра здесь
            // идут снизу вверх. Без переворота маска ложится вверх ногами:
            // ноги оказываются в небе.
            let sy = SIDE - 1 - Int((CGFloat(fy) - box.minY + offY) * CGFloat(SIDE) / th)
            if sy < 0 || sy >= SIDE { continue }
            for fx in x0..<x1 {
                let sx = Int((CGFloat(fx) - box.minX + offX) * CGFloat(SIDE) / tw)
                if sx < 0 || sx >= SIDE { continue }
                if skin[sy*SIDE + sx] {
                    acc[iy*W + fx] = 255
                    accEach[slot][iy*W + fx] = 255
                }
                if nay[sy*SIDE + sx] { nope[iy*W + fx] = 255 }
            }
        }
    }
    return true
}

accEach = Array(repeating: [UInt8](repeating: 0, count: W * H), count: models.count)

// --- проходы ----------------------------------------------------------------
// ОСНОВНОЙ проход — плитки: тот же кадр, но люди внутри входа вчетверо
// крупнее, а картинка настоящая, без вырезки и заливок. Именно вырезка
// по фигуре теряла ноги: на групповом кадре ноги пятерых девушек находились
// огрызками (4.8% кадра), плитками — целиком (8.9%).
var tiles = 0
if doTiles {
    // Делитель плитки. Половина большей стороны хороша, пока людей немного:
    // человек внутри входа становится вчетверо крупнее, а картинка остаётся
    // настоящей. На кадре с дюжиной людей в ту же плитку попадает пятеро
    // разом, и модель начинает мазать широко — в «кожу» уходят юбки, грамоты
    // и фон. Отсюда настройка: мельче плитка — крупнее человек внутри входа.
    // Размер плитки подбирается ПО ЛЮДЯМ, а не назначается раз навсегда.
    // Человек примерно в семь голов, поэтому плитка в восемь высот лица —
    // это фигура целиком плюс запас. На кадре с дюжиной девушек это даёт
    // делитель около шести, и покрытие ног растёт с 4.0% до 8.5%, а модели
    // сходятся (расхождение 2.8 -> 1.3). Если лиц нет вовсе — это само по
    // себе признак мелких людей (на снимке с шестьюдесятью детьми Vision
    // не нашёл ни одного), и плитка берётся мелкой по умолчанию.
    let longSide = max(FULL.width, FULL.height)
    var div = CGFloat(TILE_DIV)
    if TILE_DIV <= 0 {                       // 0 = подобрать самому
        let heights = faceBoxes.map { $0.height }.sorted()
        if let mid = heights.isEmpty ? nil : heights[heights.count / 2] {
            div = max(2.0, min(8.0, longSide / max(1.0, mid * 8.0)))
        } else {
            div = 4.0
        }
    }
    let side = longSide / div
    // ШАГ, то есть перекрытие плиток. Важнее, чем кажется: плитка, в которую
    // попали только ноги без головы, не опознаёт их как человека — модель ждёт
    // фигуру целиком. При шаге 0.75 такие плитки неизбежны, и маска обрывается
    // горизонтально поперёк голеней ровно по границе плитки; на снимке
    // с девушками оттуда и брались светлые полосы на ногах.
    let step = side * CGFloat(TILE_STEP)
    var y = FULL.minY
    while y < FULL.maxY {
        var x = FULL.minX
        while x < FULL.maxX {
            if pass(CGRect(x: x, y: y, width: side, height: side),
                    only: nil, aspect: 1.0) { tiles += 1 }
            x += step
        }
        y += step
    }
}

// Лица — ради мелких деталей: лоб над очками в целом кадре занимал полтора
// пикселя входа и не переживал порог, а кропом вокруг лица вернулся
// (6.0% -> 7.1% коробки).
var faces = 0
for f in faceBoxes where doFaces {
    if pass(f.insetBy(dx: -f.width * FACE_PAD, dy: -f.height * FACE_PAD), only: nil) {
        faces += 1
    }
}

// Фигуры — добавляют немного там, где Vision уверенно нашёл человека
// (на проверенных кадрах +0.2…+0.3 процентных пункта поверх плиток и лиц).
var bodies = 0
for pm in personMasks where doBodies {
    // маску тянем до размера кадра
    let sx = src.extent.width / pm.extent.width, sy = src.extent.height / pm.extent.height
    let mask = pm.transformed(by: CGAffineTransform(scaleX: sx, y: sy))
    guard let b = maskBounds(mask) else { continue }
    if pass(b.insetBy(dx: -b.width * BODY_PAD, dy: -b.height * BODY_PAD),
            only: greyOut ? mask : nil) {
        bodies += 1
    }
}

// Целый кадр одним проходом — только если не сработало НИЧЕГО (кадр меньше
// плитки, модель не отдала ни пикселя). Как основной он вреден: люди в нём
// мелкие, и на кадре с балеринами модель записала в кожу белые пачки целиком.
let noCrops = (bodies == 0 && faces == 0 && tiles == 0)
var wholeDone = (doWhole && noCrops && pass(FULL, only: nil, aspect: FRAME_ASPECT)) ? 1 : 0

// --- вычесть вето -----------------------------------------------------------
// Делается ОДИН раз в конце: пока проходы идут, «рука» от одного прохода
// и «перчатка» от другого — равноправные мнения, и кто прав, решал бы
// порядок. После всех проходов знание о перчатке сильнее: у модели для неё
// есть отдельный класс, а «рука» на её месте — ошибка масштаба.
for i in 0..<(W * H) where nope[i] != 0 {
    acc[i] = 0
    for k in accEach.indices { accEach[k][i] = 0 }
}

// --- записать ---------------------------------------------------------------
let path = (outDir as NSString).appendingPathComponent("skin.png")
let cs = CGColorSpaceCreateDeviceGray()
guard let provider = CGDataProvider(data: Data(acc) as CFData),
      let cg = CGImage(width: W, height: H, bitsPerComponent: 8, bitsPerPixel: 8,
                       bytesPerRow: W, space: cs,
                       bitmapInfo: CGBitmapInfo(rawValue: 0), provider: provider,
                       decode: nil, shouldInterpolate: false, intent: .defaultIntent)
else { fail("маска не собралась") }
if let d = ctx.pngRepresentation(of: CIImage(cgImage: cg), format: .L8, colorSpace: cs) {
    try? d.write(to: URL(fileURLWithPath: path))
} else { fail("PNG не записан") }
let share = Double(acc.reduce(0) { $0 + Int($1 > 0 ? 1 : 0) }) / Double(W*H)
print("{\"ok\":true,\"whole\":\(wholeDone),\"bodies\":\(bodies),"
    + "\"faces\":\(faces),\"tiles\":\(tiles),\"people\":true,"
    + "\"models\":[" + accEach.enumerated().map { (k, a) -> String in
        let sh = Double(a.reduce(0) { $0 + Int($1 > 0 ? 1 : 0) }) / Double(W * H)
        let nm = (modelPaths[k] as NSString).lastPathComponent
        return "{\"name\":\"\(nm)\",\"share\":\(String(format: "%.4f", sh))}"
      }.joined(separator: ",") + "],"
    + "\"skin\":\"\(path)\",\"share\":\(String(format: "%.4f", share)),"
    + "\"seconds\":\(String(format: "%.2f", -t0.timeIntervalSinceNow))}")
