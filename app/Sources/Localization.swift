// Язык интерфейса.
//
// Приложение — единственное место, где живёт русский: пайплайн под капотом
// говорит по-английски. Строки здесь только интерфейсные; всё, что описывает
// работу (что сделает прогон, подписи настроек), приходит из пайплайна уже
// на нужном языке — он же их и собирает.

import Foundation
import SwiftUI

enum Lang: String, CaseIterable, Identifiable {
    case system, ru, en
    var id: String { rawValue }

    /// Разрешённый язык: «как в системе» превращается в конкретный.
    var resolved: String {
        switch self {
        case .ru: return "ru"
        case .en: return "en"
        case .system:
            let code = Locale.preferredLanguages.first?.prefix(2).lowercased() ?? "en"
            return code == "ru" ? "ru" : "en"
        }
    }

    func title(_ lang: String) -> String {
        switch self {
        case .system: return lang == "ru" ? "Как в системе" : "Match system"
        case .ru: return "Русский"
        case .en: return "English"
        }
    }
}

@MainActor
final class L10n: ObservableObject {
    @AppStorage("interfaceLanguage") private var stored = Lang.system.rawValue

    var choice: Lang {
        get { Lang(rawValue: stored) ?? .system }
        set { stored = newValue.rawValue; objectWillChange.send() }
    }

    var code: String { choice.resolved }

    subscript(_ key: String) -> String {
        let table = code == "ru" ? L10n.ru : L10n.en
        return table[key] ?? L10n.en[key] ?? key
    }

    /// С подстановкой: t("done_n", 13)
    func t(_ key: String, _ args: CVarArg...) -> String {
        String(format: self[key], arguments: args)
    }

    static let ru: [String: String] = [
        "nav_process": "Обработка", "nav_run": "Прогон", "nav_report": "Отчёт",
        "nav_diag": "Диагностика", "nav_settings": "Настройки",
        "nav_group_work": "Работа", "nav_group_app": "Приложение",

        "main_title": "Обработка",
        "main_hint_layout": "Перетащите вёрстку InDesign или папку с фотографиями",
        "main_hint_images": "Режим картинок: без вёрстки нет фреймов, поэтому обрезка, "
                          + "даунсэмплинг и переподключение недоступны",
        "drop_empty": "Перетащите сюда файлы или папку",
        "drop_choose": "Выбрать…",
        "skipped_n": "Пропущено по фильтру: %d",
        "and_more": "… и ещё %d",
        "skipped_leftovers": "Пропущено как результат прошлых прогонов: %d "
                           + "(папки originals/ и prepress_logs/)",
        "none_matched": "Получено файлов: %d, но ни один не подошёл под этот режим "
                      + "и список обрабатываемых форматов",
        "also_images": "В наборе ещё %d картинок — переключите режим, чтобы работать с ними",
        "also_layouts": "В наборе ещё %d макетов — переключите режим, чтобы работать с ними",

        "plan_title": "Прогон сделает",
        "will_take": "В работу: %d",
        "stages_title": "Этапы",
        "stages_more": "Дополнительно",
        "only_layout": "только вёрстка",

        "btn_check": "Проверить", "btn_run": "Обработать", "btn_stop": "Остановить",
        "btn_report": "Открыть отчёт", "btn_recheck": "Проверить снова",
        "btn_install": "Установить",

        "run_title": "Прогон", "run_of": "%d из %d",
        "run_done": "Обработано %d, предупреждений %d",
        "run_failed": "Прогон не удался",
        "log_title": "Журнал",

        "report_title": "Отчёт", "report_none": "Отчёта ещё нет — запустите прогон.",
        "report_open_finder": "Показать в Finder",

        "diag_title": "Диагностика",
        "diag_hint": "Что стоит на этом Mac и чего не хватает.",
        "diag_ok": "на месте", "diag_missing": "не установлен",
        "diag_installing": "Ставится через Homebrew…",

        "set_title": "Настройки",
        "set_language": "Язык интерфейса",

        "filter_version": "Версия InDesign", "filter_suffix": "Только с суффиксом",
        "filter_any": "любая",
        "mode_layout": "Вёрстка", "mode_images": "Картинки",
    ]

    static let en: [String: String] = [
        "nav_process": "Process", "nav_run": "Run", "nav_report": "Report",
        "nav_diag": "Diagnostics", "nav_settings": "Settings",
        "nav_group_work": "Work", "nav_group_app": "App",

        "main_title": "Process",
        "main_hint_layout": "Drop an InDesign layout or a folder of photos",
        "main_hint_images": "Images mode: no layout means no frames, so crop, "
                          + "downsampling and relinking are unavailable",
        "drop_empty": "Drop files or a folder here",
        "drop_choose": "Choose…",
        "skipped_n": "Skipped by filter: %d",
        "and_more": "… and %d more",
        "skipped_leftovers": "%d file(s) skipped as results of earlier runs "
                           + "(originals/ and prepress_logs/)",
        "none_matched": "%d file(s) received, none match this mode and the list "
                      + "of supported formats",
        "also_images": "%d images are also here — switch the mode to work with them",
        "also_layouts": "%d layouts are also here — switch the mode to work with them",

        "plan_title": "This run will",
        "will_take": "To process: %d",
        "stages_title": "Stages",
        "stages_more": "More stages",
        "only_layout": "layout only",

        "btn_check": "Check", "btn_run": "Process", "btn_stop": "Stop",
        "btn_report": "Open report", "btn_recheck": "Check again",
        "btn_install": "Install",

        "run_title": "Run", "run_of": "%d of %d",
        "run_done": "Processed %d, warnings %d",
        "run_failed": "The run failed",
        "log_title": "Log",

        "report_title": "Report", "report_none": "No report yet — start a run.",
        "report_open_finder": "Reveal in Finder",

        "diag_title": "Diagnostics",
        "diag_hint": "What this Mac has and what is missing.",
        "diag_ok": "present", "diag_missing": "not installed",
        "diag_installing": "Installing via Homebrew…",

        "set_title": "Settings",
        "set_language": "Interface language",

        "filter_version": "InDesign version", "filter_suffix": "Suffix only",
        "filter_any": "any",
        "mode_layout": "Layout", "mode_images": "Images",
    ]
}
