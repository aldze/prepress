// Маски и разметка кадра средствами Apple Vision — всё, ради чего в варианте
// с Photoshop приходилось звать Photoshop.
//
// Один запуск — один разбор файла и сразу три ответа:
//   * маска людей (PNG, серый) — аналог Select People;
//   * маска КАЖДОГО человека отдельно — там, где Vision умеет их разделить;
//   * маска переднего плана — ею из неба и зелени вычитается всё, что не фон;
//   * прямоугольники лиц — по ним кожа меряется по каждому человеку отдельно;
//   * ярлыки сюжета — ворота, которых у Photoshop нет вовсе: Select Sky
//     уверенно отдаёт белый потолок в интерьере, а классификатор на том же
//     кадре ярлыка sky не ставит.
//
// Почему один процесс на всё: VNImageRequestHandler декодирует файл один раз,
// и три запроса к нему обходятся дешевле трёх запусков. На кадре 2000 px это
// около 1.5 с против 9-25 с у Photoshop на те же маски.
//
// Долю кадра под маской инструмент намеренно не считает: CIAreaAverage
// усредняет уже в sRGB, и линейная четверть кадра возвращалась как 0.53
// (0.25 в степени 1/2.2). Площадь меряет magick по готовому PNG — там
// гамма под контролем.
//
// Ответ уходит в stdout одной строкой JSON. Ошибка — тоже JSON, с "ok": false:
// разбирающей стороне не приходится гадать, что пришло.
import Foundation
import Vision
import CoreImage

func fail(_ why: String) -> Never {
    let esc = why.replacingOccurrences(of: "\\", with: "\\\\")
                 .replacingOccurrences(of: "\"", with: "\\\"")
    print("{\"ok\":false,\"error\":\"\(esc)\"}")
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 3 else { fail("vmask <вход> <папка> [макс_сторона]") }
let inPath = args[1]
let outDir = args[2]
let maxSide = args.count > 3 ? (Int(args[3]) ?? 0) : 0

let url = URL(fileURLWithPath: inPath)
guard let src = CIImage(contentsOf: url) else { fail("файл не прочитан: \(inPath)") }
let W = src.extent.width, H = src.extent.height
let t0 = Date()

// Vision берёт файл по URL сам: так он и декодирует, и ориентацию EXIF
// применяет без нашего участия. Раньше здесь передавался CIImage, и кадры,
// снятые вертикально, приходили лежащими — лица не находились вовсе.
let handler = VNImageRequestHandler(url: url, options: [:])

let personReq = VNGeneratePersonSegmentationRequest()
let instReq = VNGeneratePersonInstanceMaskRequest()
let fgReq = VNGenerateForegroundInstanceMaskRequest()
personReq.qualityLevel = .accurate
personReq.outputPixelFormat = kCVPixelFormatType_OneComponent8
let faceReq = VNDetectFaceLandmarksRequest()
let poseReq = VNDetectHumanBodyPoseRequest()
let classReq = VNClassifyImageRequest()

do {
    try handler.perform([personReq, faceReq, classReq])
} catch {
    fail("Vision не отработал: \(error.localizedDescription)")
}

// Размер, до которого тянутся все маски: либо кадр, либо заданный предел.
// Дальше с ними работает magick, и лишние пиксели ему только в тягость.
var TW = W, TH = H
if maxSide > 0 && max(W, H) > CGFloat(maxSide) {
    let k = CGFloat(maxSide) / max(W, H)
    TW = (W * k).rounded(); TH = (H * k).rounded()
}
let ctx = CIContext()
let grayCS = CGColorSpaceCreateDeviceGray()

func writeMask(_ img: CIImage, _ path: String) -> Bool {
    var m = img
    m = m.transformed(by: CGAffineTransform(scaleX: TW / m.extent.width,
                                            y: TH / m.extent.height))
    guard let data = ctx.pngRepresentation(of: m, format: .L8, colorSpace: grayCS)
    else { return false }
    do { try data.write(to: URL(fileURLWithPath: path)); return true }
    catch { return false }
}

// --- маска людей -----------------------------------------------------------
var personPath = "null"
if let obs = personReq.results?.first {
    let path = (outDir as NSString).appendingPathComponent("person.png")
    if writeMask(CIImage(cvPixelBuffer: obs.pixelBuffer), path) {
        personPath = "\"\(path)\""
    } else {
        fail("маска людей не записана")
    }
}

// --- каждый человек отдельной маской ---------------------------------------
// Там, где Vision умеет их разделить. Умеет не всегда: на плотной группе
// из шести человек он отдал три экземпляра — перекрывающихся слепляет.
// Поэтому это не замена лицам, а дополнение: разбирающая сторона сама решает,
// хватило ли экземпляров, и при нехватке делит их дальше по лицам.
//
// ВНИМАНИЕ: ПЕРВЫЙ в жизни системы вызов занимает минуты — модель
// подгружается и компилируется. Дальше около полусекунды на кадр.
var instances: [String] = []
do {
    try handler.perform([instReq])
    if let o = instReq.results?.first {
        for (i, inst) in o.allInstances.enumerated() {
            guard let buf = try? o.generateScaledMaskForImage(forInstances: [inst],
                                                              from: handler) else { continue }
            let path = (outDir as NSString).appendingPathComponent("person_\(i + 1).png")
            if writeMask(CIImage(cvPixelBuffer: buf), path) {
                instances.append("\"\(path)\"")
            }
        }
    }
} catch { /* нет людей или модель недоступна — не беда, есть общая маска */ }

// --- передний план ----------------------------------------------------------
// Небо и трава — это фон. Вычтя передний план, перестаём принимать за траву
// зелёную рубашку, а за небо — голубые джинсы.
var subjectPath = "null"
do {
    try handler.perform([fgReq])
    if let o = fgReq.results?.first,
       let buf = try? o.generateScaledMaskForImage(forInstances: o.allInstances,
                                                   from: handler) {
        let path = (outDir as NSString).appendingPathComponent("subject.png")
        if writeMask(CIImage(cvPixelBuffer: buf), path) { subjectPath = "\"\(path)\"" }
    }
} catch { /* субъекта нет — маска не нужна */ }

// --- контуры лиц и суставы тела ---------------------------------------------
//
// Зачем. Цвет не отличает кожу от ткани телесного оттенка — это предел метода,
// а не настройки: на кадре с пятью людьми «кожей» становилось 15% кадра,
// включая белые кофты и бежевые штаны. Анатомия отвечает на вопрос иначе:
// контур лица очерчивает лицо без волос и воротника, а суставы задают руки
// и ноги. Торс, где живёт одежда, в область кожи не попадает вовсе.
//
// Разбора человека на части тела у Vision нет (проверено по заголовкам SDK),
// но лицо и скелет есть, и этого хватает, чтобы очертить кожу по строению,
// а цвет оставить уточняющим ситом внутри.
var contours: [String] = []
for f in (faceReq.results ?? []) {
    guard let lm = f.landmarks?.faceContour else { continue }
    let b = f.boundingBox
    var pts: [String] = []
    for p in lm.normalizedPoints {
        // Точки ориентира заданы в долях ПРЯМОУГОЛЬНИКА ЛИЦА, а не кадра.
        let ix = b.origin.x + CGFloat(p.x) * b.width
        let iy = b.origin.y + CGFloat(p.y) * b.height
        pts.append(String(format: "[%.4f,%.4f]", ix, 1.0 - iy))
    }
    if !pts.isEmpty { contours.append("[" + pts.joined(separator: ",") + "]") }
}

var bodies: [String] = []
do {
    try handler.perform([poseReq])
    for obs in (poseReq.results ?? []) {
        guard let pts = try? obs.recognizedPoints(.all) else { continue }
        var js: [String] = []
        for (name, p) in pts where p.confidence > 0.2 {
            let key = name.rawValue.rawValue
            js.append("\"" + key + "\":["
                      + String(format: "%.4f,%.4f,%.2f",
                               p.location.x, 1.0 - p.location.y, p.confidence) + "]")
        }
        if !js.isEmpty { bodies.append("{" + js.joined(separator: ",") + "}") }
    }
} catch { /* людей нет — не беда */ }

// --- лица ------------------------------------------------------------------
// Vision отдаёт координаты снизу вверх и в долях кадра. Наружу выдаём сверху
// вниз: дальше с ними работает magick, у которого начало координат в углу
// слева сверху, и переворачивать дважды незачем.
var faces: [String] = []
for f in (faceReq.results ?? []) {
    let b = f.boundingBox
    faces.append(String(format: "{\"x\":%.4f,\"y\":%.4f,\"w\":%.4f,\"h\":%.4f}",
                        b.origin.x, 1.0 - b.origin.y - b.height, b.width, b.height))
}

// --- сюжет -----------------------------------------------------------------
var labels: [String] = []
for r in (classReq.results ?? []) where r.confidence >= 0.05 {
    let id = r.identifier.replacingOccurrences(of: "\"", with: "")
    labels.append(String(format: "\"%@\":%.3f", id, r.confidence))
}

let dt = -t0.timeIntervalSinceNow
print("{\"ok\":true,\"w\":\(Int(W)),\"h\":\(Int(H)),"
    + "\"person\":\(personPath),"
    + "\"persons\":[\(instances.joined(separator: ","))],"
    + "\"subject\":\(subjectPath),"
    + "\"faces\":[\(faces.joined(separator: ","))],"
    + "\"faceContours\":[\(contours.joined(separator: ","))],"
    + "\"bodies\":[\(bodies.joined(separator: ","))],"
    + "\"labels\":{\(labels.joined(separator: ","))},"
    + "\"seconds\":\(String(format: "%.2f", dt))}")
