// Настройки и состав цепочки — через сам пайплайн.
//
// Приложение НЕ держит своей копии настроек и не пишет config.json само.
// Оно спрашивает `--config-menu` и пишет через `--config-set`. Причина
// не в лени: у config.json есть проверка допустимых сочетаний, и она
// живёт в Python. Своя копия рано или поздно разойдётся с ней — и тогда
// приложение сохранит настройку, на которой прогон падает посреди номера.
//
// Формат обмена — табуляции, тот же, что уже читает дроплет:
//   ключ \t раздел \t подпись \t значение \t тип \t варианты

import Foundation

struct SettingField: Identifiable, Hashable {
    let key: String
    let section: String
    let label: String
    let value: String
    let kind: String            // bool | choice | number | file | mode | text
    let options: [(String, String)]

    var id: String { key }

    // Сравнение ПО ВСЕМ полям, и это не педантизм. SwiftUI решает, стоит ли
    // перерисовывать вид, сравнивая его свойства, а для Equatable-типа берёт
    // именно ==. Пока здесь стоял один ключ, строка настройки с новым
    // значением считалась той же самой: пайплайн записывал выбор, config.json
    // менялся, а меню на экране отщёлкивало обратно на старый вариант —
    // выглядело как «переключатель не работает».
    static func == (a: SettingField, b: SettingField) -> Bool {
        a.key == b.key && a.value == b.value && a.label == b.label
            && a.kind == b.kind && a.section == b.section
            && a.options.count == b.options.count
            && zip(a.options, b.options).allSatisfy { $0.0 == $1.0 && $0.1 == $1.1 }
    }

    // А вот хэш остаётся по ключу: он адресует настройку, и в множестве
    // две записи одного ключа — это одна настройка, как бы ни разошлись
    // значения. Контракт «равные объекты дают равный хэш» это не нарушает.
    func hash(into h: inout Hasher) { h.combine(key) }

    var isOn: Bool { value == "да" || value == "yes" }
}

/// Что прогон сделает с текущими настройками — из `--run-info`.
/// Текст приходит уже на нужном языке: пайплайн его и собирает.
struct RunPlan {
    var kind: String = "layout"   // layout | images
    var chain: [String] = []
    var lines: [String] = []

    var acceptsLayouts: Bool { kind == "layout" }
}

@MainActor
final class Settings: ObservableObject {
    @Published private(set) var fields: [SettingField] = []
    @Published private(set) var plan = RunPlan()
    @Published var lastError: String?

    /// Язык интерфейса. Пайплайну передаётся явно: он сам не догадывается,
    /// на каком языке говорит приложение.
    var lang: String = "ru"

    var sections: [String] {
        var seen = Set<String>(), out: [String] = []
        for f in fields where !seen.contains(f.section) {
            seen.insert(f.section); out.append(f.section)
        }
        return out
    }

    func fields(in section: String) -> [SettingField] {
        fields.filter { $0.section == section }
    }

    func field(_ key: String) -> SettingField? {
        fields.first { $0.key == key }
    }

    /// Состав цепочки, который приложение отправит в прогон. Задаётся
    /// извне — экраном, который знает и режим, и состояние карточек.
    var stagesForRun: [String] = []

    func reload() {
        if let text = PipelineQuery.run(["--config-menu", "--lang", lang]) {
            fields = Settings.parseMenu(text)
        }
        // План спрашиваем ТЕМИ ЖЕ аргументами, с какими пойдёт прогон:
        // иначе строка внизу обещает одно, а делается другое.
        var args = ["--run-info", "--lang", lang]
        if !stagesForRun.isEmpty {
            args += ["--stages", stagesForRun.joined(separator: ",")]
        }
        if let text = PipelineQuery.run(args) {
            plan = Settings.parsePlan(text)
        }
    }

    /// Показать новое значение НЕМЕДЛЕННО, не дожидаясь пайплайна.
    /// Запись занимает доли секунды, но SwiftUI успевает отрисовать за это
    /// время ещё старое состояние, и тумблер дёргается туда-сюда. Правда
    /// приедет следом: reload() перечитает меню и поправит, если пайплайн
    /// решил иначе (отказал в сочетании, подтянул соседний ключ).
    private func showAtOnce(_ key: String, _ value: String) {
        guard let i = fields.firstIndex(where: { $0.key == key }) else { return }
        let f = fields[i]
        let shown: String
        if f.kind == "bool" {
            let on = (value == "да" || value == "yes")
            shown = lang == "ru" ? (on ? "да" : "нет") : (on ? "yes" : "no")
        } else if !f.options.isEmpty {
            // В значении лежит ПОДПИСЬ варианта, а пишем мы код: переводим.
            shown = f.options.first { $0.0 == value }?.1 ?? value
        } else {
            shown = value
        }
        fields[i] = SettingField(key: f.key, section: f.section, label: f.label,
                                 value: shown, kind: f.kind, options: f.options)
    }

    /// Записать настройку. Пайплайн отвергает недопустимое сочетание —
    /// показываем его объяснение, а не пытаемся угадать сами.
    @discardableResult
    func set(_ key: String, _ value: String) -> Bool {
        showAtOnce(key, value)
        let p = Process()
        let paths = PipelinePaths.locate()
        p.executableURL = paths.python
        p.arguments = [paths.script.path, "--config-set", "\(key)=\(value)"]
        let out = Pipe(), err = Pipe()
        p.standardOutput = out; p.standardError = err
        do { try p.run() } catch {
            lastError = error.localizedDescription; return false
        }
        let outText = String(data: out.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
        let errText = String(data: err.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
        p.waitUntilExit()
        if p.terminationStatus != 0 {
            // В stderr приходит объяснение: чего не хватает и что включить.
            lastError = (errText.isEmpty ? outText : errText)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return false
        }
        lastError = nil
        reload()
        return true
    }

    func toggleStage(_ stage: String, on: Bool) {
        set("stage.\(stage)", on ? "да" : "нет")
    }

    // MARK: - Разбор

    static func parseMenu(_ text: String) -> [SettingField] {
        text.split(separator: "\n").compactMap { line in
            let p = line.components(separatedBy: "\t")
            guard p.count >= 5 else { return nil }
            let options = p.count >= 6 && !p[5].isEmpty
                ? p[5].components(separatedBy: ";").compactMap { pair -> (String, String)? in
                    let kv = pair.components(separatedBy: "=")
                    return kv.count >= 2 ? (kv[0], kv.dropFirst().joined(separator: "=")) : nil
                  }
                : []
            return SettingField(key: p[0], section: p[1], label: p[2],
                                value: p[3], kind: p[4], options: options)
        }
    }

    static func parsePlan(_ text: String) -> RunPlan {
        var plan = RunPlan()
        for line in text.split(separator: "\n") {
            let p = line.components(separatedBy: "\t")
            guard p.count >= 2 else { continue }
            switch p[0] {
            case "kind":  plan.kind = p[1]
            case "chain": plan.chain = p[1].components(separatedBy: " -> ")
            case "line":  plan.lines.append(p[1])
            default: break
            }
        }
        return plan
    }
}
