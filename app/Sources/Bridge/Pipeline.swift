// Мост к пайплайну: запуск prepress.py и разбор его событий.
//
// Вся логика обработки остаётся в Python. Приложение её не повторяет и
// не знает, как устроен прогон, — оно запускает процесс и читает поток
// событий. Так пайплайн остаётся ОДИН: из терминала, из приложения и из
// тестов работает тот же код. Самодельная копия разошлась бы с ним
// на третьей мелочи — это в проекте уже проверено дорогой ценой.
//
// Разговор идёт строками JSON в stdout (--json-events), а не разбором
// человеческого лога регулярками, как делал дроплет: поправишь
// формулировку в логе — и индикатор снаружи молча ломается.

import Foundation

// MARK: - События

/// Событие прогона. Поля необязательные: набор зависит от вида события,
/// и требовать их все значило бы ронять разбор из-за лишней запятой.
struct RunEvent: Decodable {
    let event: String
    // run_start
    var document: String?
    var onFiles: Bool?
    var stages: [String]?
    var engine: String?
    var dryRun: Bool?
    // image
    var index: Int?
    var total: Int?
    var name: String?
    var path: String?
    // stage
    var what: String?
    // run_done
    var seconds: Double?
    var processed: Int?
    var failed: Int?
    var warnings: Int?
    var report: String?
    var reportHtml: String?
    var manifest: String?
    var log: String?
    var savedTo: String?
}

// MARK: - Что показывать во время прогона

@MainActor
final class RunState: ObservableObject {
    @Published var isRunning = false
    @Published var document = ""
    @Published var stage = ""
    @Published var imageIndex = 0
    @Published var imageTotal = 0
    @Published var imageName = ""
    @Published var logTail: [String] = []
    @Published var finished: RunEvent?
    @Published var failure: String?

    /// Хвост журнала: показываем последние строки, а не весь файл.
    /// Прогон номера пишет тысячи строк, и держать их в памяти незачем —
    /// полный журнал всегда лежит на диске.
    private let tailLimit = 400

    func reset() {
        isRunning = true; stage = ""; imageIndex = 0; imageTotal = 0
        imageName = ""; logTail = []; finished = nil; failure = nil
    }

    func apply(_ e: RunEvent) {
        switch e.event {
        case "run_start":
            document = e.document.map { (_ p: String) -> String in
                (p as NSString).lastPathComponent
            } ?? ""
        case "image":
            imageIndex = e.index ?? 0
            imageTotal = e.total ?? 0
            imageName = e.name ?? ""
        case "stage":
            stage = e.what ?? ""
        case "run_done":
            // run_done приходит на КАЖДЫЙ макет, а не на весь прогон: пачка
            // из нескольких .indd идет одним процессом. Снимать здесь
            // isRunning нельзя — после первого макета пропадала кнопка
            // «Остановить», хотя следующий уже обрабатывался. Конец прогона —
            // это выход процесса, его ловит onFinish.
            finished = e
        default:
            break
        }
    }

    func appendLog(_ line: String) {
        logTail.append(line)
        if logTail.count > tailLimit {
            logTail.removeFirst(logTail.count - tailLimit)
        }
    }

    var progress: Double {
        imageTotal > 0 ? Double(imageIndex) / Double(imageTotal) : 0
    }
}

// MARK: - Запуск

enum PipelineError: LocalizedError {
    case pythonMissing(String)
    case scriptMissing(String)
    case failed(code: Int32, tail: String)

    var errorDescription: String? {
        switch self {
        case .pythonMissing(let p): return "Python не найден: \(p)"
        case .scriptMissing(let p): return "prepress.py не найден: \(p)"
        case .failed(let code, let tail):
            return tail.isEmpty ? "Прогон завершился с кодом \(code)" : tail
        }
    }
}

/// Где лежат Python и пайплайн. В отладочной сборке — рядом с исходниками,
/// в готовом приложении — внутри бандла.
struct PipelinePaths {
    var python: URL
    var script: URL
    var toolsDir: URL

    static func locate() -> PipelinePaths {
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("pipeline")
        let root: URL
        if let b = bundled, FileManager.default.fileExists(
            atPath: b.appendingPathComponent("prepress.py").path) {
            root = b
        } else {
            // Отладка: приложение запущено из сборки рядом с репозиторием.
            root = URL(fileURLWithPath: developmentRoot())
        }
        // Помощники (vtone, vmask, vskin) и модель разбора человека.
        // Внутри бандла — свои; иначе те, что положил setup.sh. Имя папки
        // НЕ выдумывать: пайплайн и setup.sh знают именно это место, и своё
        // название означало бы пустую папку и молча пропущенные этапы.
        let fm = FileManager.default
        let inBundle = Bundle.main.resourceURL?.appendingPathComponent("tools")
        let installed = fm.urls(for: .applicationSupportDirectory,
                                in: .userDomainMask)[0]
            .appendingPathComponent("prepress_automation/tools")
        let tools = (inBundle.map { fm.fileExists(atPath: $0.path) } ?? false)
            ? inBundle! : installed
        return PipelinePaths(python: URL(fileURLWithPath: "/usr/bin/python3"),
                             script: root.appendingPathComponent("prepress.py"),
                             toolsDir: tools)
    }

    /// Корень репозитория при запуске из сборки. Путь подставляется
    /// скриптом сборки — константы в исходнике быть не должно, как
    /// у @@PREPRESS_PY@@ в дроплете.
    private static func developmentRoot() -> String {
        "@@REPO_ROOT@@"
    }
}

/// Один прогон пайплайна.
///
/// stdout разбирается построчно: строка, начинающаяся с «{», — событие,
/// всё прочее — журнал. Разделять их по потокам (события в stdout, лог
/// в stderr) было бы чище, но лог пишется ещё и в файл, и дублировать
/// его в stderr значит удваивать работу на каждой строке.
final class PipelineRun {
    private let process = Process()
    private let out = Pipe()
    private let err = Pipe()
    private var stderrTail: [String] = []

    let paths: PipelinePaths

    init(paths: PipelinePaths = .locate()) {
        self.paths = paths
    }

    func cancel() {
        if process.isRunning { process.terminate() }
    }

    /// Запускает прогон. onEvent и onLog зовутся на главной очереди.
    func start(arguments: [String],
               onEvent: @escaping @MainActor (RunEvent) -> Void,
               onLog: @escaping @MainActor (String) -> Void,
               onFinish: @escaping @MainActor (Result<Void, Error>) -> Void) {
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: paths.python.path) else {
            Task { @MainActor in onFinish(.failure(
                PipelineError.pythonMissing(paths.python.path))) }
            return
        }
        guard fm.fileExists(atPath: paths.script.path) else {
            Task { @MainActor in onFinish(.failure(
                PipelineError.scriptMissing(paths.script.path))) }
            return
        }

        process.executableURL = paths.python
        process.arguments = [paths.script.path, "--json-events", "--no-open"] + arguments
        process.standardOutput = out
        process.standardError = err

        var env = ProcessInfo.processInfo.environment
        env["PREPRESS_TOOLS_DIR"] = paths.toolsDir.path
        // Потолок памяти для magick. По умолчанию ему разрешена ВСЯ память
        // машины, и на кадре 5464x8192 в 16 битах прогон её съедал — систему
        // выбивало на 42-й картинке из 111. С потолком magick досчитывает
        // на диске, что заметно медленнее, но переживается.
        env["MAGICK_MEMORY_LIMIT"] = "3GiB"
        env["MAGICK_MAP_LIMIT"] = "6GiB"
        // Python не должен буферизовать вывод: иначе события приходят
        // пачкой в конце, и индикатор стоит на месте весь прогон.
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env

        readLines(from: out) { line in
            if line.hasPrefix("{"), let data = line.data(using: .utf8),
               let e = try? JSONDecoder().decode(RunEvent.self, from: data) {
                Task { @MainActor in onEvent(e) }
            } else if !line.isEmpty {
                Task { @MainActor in onLog(line) }
            }
        }
        readLines(from: err) { [weak self] line in
            guard !line.isEmpty else { return }
            self?.stderrTail.append(line)
            if let n = self?.stderrTail.count, n > 40 { self?.stderrTail.removeFirst() }
            Task { @MainActor in onLog(line) }
        }

        process.terminationHandler = { [weak self] p in
            let tail = (self?.stderrTail ?? []).suffix(8).joined(separator: "\n")
            Task { @MainActor in
                onFinish(p.terminationStatus == 0 ? .success(())
                         : .failure(PipelineError.failed(code: p.terminationStatus,
                                                         tail: tail)))
            }
        }

        do {
            try process.run()
        } catch {
            Task { @MainActor in onFinish(.failure(error)) }
        }
    }

    /// Чтение по строкам. Собственный буфер нужен потому, что канал отдаёт
    /// произвольные куски: строка JSON может прийти разрезанной пополам,
    /// и разбор такого куска молча потеряет событие.
    private func readLines(from pipe: Pipe, _ handle: @escaping (String) -> Void) {
        var buffer = Data()
        pipe.fileHandleForReading.readabilityHandler = { fh in
            let chunk = fh.availableData
            if chunk.isEmpty {
                if !buffer.isEmpty, let s = String(data: buffer, encoding: .utf8) {
                    handle(s)
                }
                fh.readabilityHandler = nil
                return
            }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                let line = buffer[buffer.startIndex..<nl]
                buffer.removeSubrange(buffer.startIndex...nl)
                if let s = String(data: line, encoding: .utf8) { handle(s) }
            }
        }
    }
}

// MARK: - Разовые запросы к пайплайну

/// Короткий синхронный вызов: --run-info, --list-stages, --config-menu.
/// Прогон так запускать нельзя — он идёт минутами.
enum PipelineQuery {
    static func run(_ arguments: [String],
                    paths: PipelinePaths = .locate()) -> String? {
        let p = Process()
        p.executableURL = paths.python
        p.arguments = [paths.script.path] + arguments
        var env = ProcessInfo.processInfo.environment
        env["PREPRESS_TOOLS_DIR"] = paths.toolsDir.path
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(data: data, encoding: .utf8)
    }
}
