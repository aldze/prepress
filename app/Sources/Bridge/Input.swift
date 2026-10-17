// Что бросили на приложение: макеты или картинки.
//
// Разбор идёт ДО запуска пайплайна: человек должен увидеть, что именно
// возьмут в работу, прежде чем нажмёт «Обработать». Смешивать макеты
// и картинки нельзя — у них разные цепочки и разные правила записи;
// пайплайн такое отвергает, и приложение не должно доводить до отказа.

import Foundation

/// Чем работаем. Это ВЫБОР ЧЕЛОВЕКА, а не вывод из содержимого папки:
/// папка вёрстки всегда содержит и .indd, и картинки в Links — «смешанный
/// вход» тут норма, а не ошибка. Выводить режим из содержимого значило
/// заводить приложение в тупик, где ни один вариант не выбран и сделать
/// ничего нельзя.
enum InputKind: Hashable {
    case layouts, images
}

struct InputItem: Identifiable, Hashable {
    let url: URL
    var indesignVersion: String?   // только для .indd
    var linkCount: Int?
    var skipped: String?           // почему не берём

    var id: URL { url }
    var name: String { url.lastPathComponent }
    var isLayout: Bool { url.pathExtension.lowercased() == "indd" }
}

@MainActor
final class InputSet: ObservableObject {
    @Published private(set) var items: [InputItem] = []
    /// Режим переключается ПО СОДЕРЖИМОМУ, но только когда содержимое
    /// однозначно: одни макеты — «вёрстка», одни картинки — «картинки».
    /// Смешанный набор не трогает переключатель вовсе — там выбор за
    /// человеком, и угадывать нечего. Прежний вариант («предлагать, пока
    /// человек не трогал сам») заводил в тупик с другой стороны: бросил
    /// папку картинок при выбранной «вёрстке» — и список молча пуст.
    @Published var kind: InputKind = .layouts { didSet { reapply() } }

    /// Переключение человеком.
    func choose(_ k: InputKind) { kind = k }

    /// Фильтры из ТЗ. Пустые — не фильтруем.
    @Published var versionFilter: String = "" { didSet { reapply() } }
    @Published var suffixFilter: String = "" { didSet { reapply() } }

    private var raw: [URL] = []

    /// Сколько файлов отброшено как результат прошлых прогонов
    /// (папки originals/ и prepress_logs/). Молчать об этом нельзя:
    /// человек бросает файл, список остаётся пустым, и поведение
    /// выглядит зависанием, а не отказом.
    @Published private(set) var excluded: Int = 0

    /// Сколько файлов вообще дошло до разбора. Нужно, чтобы отличить
    /// «ничего не бросили» от «бросили, но всё отсеялось»: молчащий список
    /// выглядит одинаково в обоих случаях, и человеку нечего чинить.
    var rawCount: Int { raw.count }

    var accepted: [InputItem] { items.filter { $0.skipped == nil } }
    var skipped: [InputItem] { items.filter { $0.skipped != nil } }

    /// Расширения растра — те же, что в config.json (raster_extensions).
    /// Список продублирован намеренно: спрашивать пайплайн на каждый
    /// брошенный файл дороже, чем держать копию, а расходится он редко.
    static let rasterExtensions: Set<String> = [
        "jpg", "jpeg", "jpe", "tif", "tiff", "png", "psd",
        "bmp", "webp", "avif", "heic", "heif"
    ]

    func accept(_ urls: [URL]) {
        let (found, hidden) = InputSet.expand(urls)
        raw = found
        excluded = hidden
        // Однородный набор сам говорит, чем работать; смешанный молчит.
        let layouts = raw.contains { $0.pathExtension.lowercased() == "indd" }
        let images = raw.contains { $0.pathExtension.lowercased() != "indd" }
        if layouts && !images { kind = .layouts }
        else if images && !layouts { kind = .images }
        reapply()
    }

    func clear() {
        raw = []; items = []; excluded = 0
    }

    /// Сколько файлов другого рода лежит в том же наборе — чтобы показать
    /// «а ещё тут 112 картинок», и человек знал, что переключатель не зря.
    var otherCount: Int {
        raw.filter { ($0.pathExtension.lowercased() == "indd") != (kind == .layouts) }.count
    }

    private func reapply() {
        var out: [InputItem] = []
        // Отбираем по режиму: в режиме вёрстки берём только .indd,
        // в режиме картинок — только растр. Так папка номера годится
        // и для того, и для другого, а выбирает человек.
        for url in raw where (url.pathExtension.lowercased() == "indd") == (kind == .layouts) {
            var item = InputItem(url: url)
            if item.isLayout {
                item.indesignVersion = InDesignFile.version(of: url)
                if !versionFilter.isEmpty,
                   item.indesignVersion != versionFilter {
                    item.skipped = "версия \(item.indesignVersion ?? "?")"
                }
                if !suffixFilter.isEmpty {
                    let stem = url.deletingPathExtension().lastPathComponent
                    if !stem.hasSuffix(suffixFilter) {
                        item.skipped = "без суффикса \(suffixFilter)"
                    }
                }
            }
            out.append(item)
        }
        items = out
    }

    /// Разворачивает папки. Картинки ищем рекурсивно (так в ТЗ), макеты —
    /// тоже: папка номера обычно и есть папка с макетами.
    private static func expand(_ urls: [URL]) -> ([URL], Int) {
        let fm = FileManager.default
        var out: [URL] = []
        var hidden = 0
        for url in urls {
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: url.path, isDirectory: &isDir) else { continue }
            if !isDir.boolValue {
                if isInteresting(url) { out.append(url) }
                else if isLeftover(url) { hidden += 1 }
                continue
            }
            guard let e = fm.enumerator(at: url,
                                        includingPropertiesForKeys: [.isRegularFileKey],
                                        options: [.skipsHiddenFiles,
                                                  .skipsPackageDescendants]) else { continue }
            for case let child as URL in e {
                if isInteresting(child) { out.append(child) }
                else if isLeftover(child) { hidden += 1 }
            }
        }
        // Порядок как в Finder, иначе список выглядит случайным.
        return (out.sorted {
            $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending
        }, hidden)
    }

    /// Файл из прошлого прогона: наш собственный результат или журнал.
    private static func isLeftover(_ url: URL) -> Bool {
        guard url.path.contains("/originals/") || url.path.contains("/prepress_logs/")
        else { return false }
        let ext = url.pathExtension.lowercased()
        return ext == "indd" || rasterExtensions.contains(ext)
    }

    private static func isInteresting(_ url: URL) -> Bool {
        let ext = url.pathExtension.lowercased()
        if ext == "indd" { return true }
        // Служебные файлы прогона в работу не берём: иначе повторный проход
        // по той же папке начнёт обрабатывать собственные результаты.
        if url.path.contains("/prepress_logs/") || url.path.contains("/originals/") {
            return false
        }
        return rasterExtensions.contains(ext)
    }
}

// MARK: - Версия .indd

/// Версия InDesign, сохранившего файл, — из заголовка, без запуска InDesign.
///
/// В начале .indd лежит блок метаданных, где среди прочего есть строка
/// вида «Adobe InDesign 20.0» или номер формата. Читаем первые килобайты
/// и ищем её: открывать документ ради номера версии значит ждать минуту
/// и получить модальное окно конверсии на чужой версии.
enum InDesignFile {
    static func version(of url: URL) -> String? {
        guard let fh = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? fh.close() }
        guard let head = try? fh.read(upToCount: 64 * 1024), !head.isEmpty else {
            return nil
        }
        // Метаданные лежат в UTF-16 и в ASCII вперемешку, поэтому ищем
        // в обоих прочтениях: в UTF-16 буквы разделены нулевыми байтами,
        // и поиск по ASCII-строке их не найдёт.
        if let v = firstVersion(in: String(decoding: head, as: UTF8.self)) {
            return v
        }
        let words: [UInt16] = stride(from: 0, to: head.count - 1, by: 2).map {
            UInt16(head[head.startIndex + $0]) |
            (UInt16(head[head.startIndex + $0 + 1]) << 8)
        }
        return firstVersion(in: String(decoding: words, as: UTF16.self))
    }

    /// «Adobe InDesign 20.0» -> «2025»: пользователь знает год, а не
    /// внутренний номер. Соответствие меняется раз в год и живёт здесь.
    private static let years: [String: String] = [
        "16": "2021", "17": "2022", "18": "2023",
        "19": "2024", "20": "2025", "21": "2026",
    ]

    private static func firstVersion(in text: String) -> String? {
        guard let r = text.range(of: #"Adobe InDesign[^0-9]{0,8}(\d{1,2})"#,
                                 options: .regularExpression) else { return nil }
        let digits = text[r].filter { $0.isNumber }
        return years[String(digits.prefix(2))] ?? String(digits.prefix(2))
    }
}
