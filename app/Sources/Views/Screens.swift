// Экраны приложения: обработка, прогон, отчёт, диагностика, настройки.

import SwiftUI
import WebKit
import UniformTypeIdentifiers

enum Screen: String, CaseIterable, Identifiable {
    case process, run, report, diag, settings
    var id: String { rawValue }

    var icon: String {
        switch self {
        case .process:  return "photo.on.rectangle.angled"
        case .run:      return "clock"
        case .report:   return "doc.text"
        case .diag:     return "checkmark.shield"
        case .settings: return "gearshape"
        }
    }
    var titleKey: String {
        switch self {
        case .process:  return "nav_process"
        case .run:      return "nav_run"
        case .report:   return "nav_report"
        case .diag:     return "nav_diag"
        case .settings: return "nav_settings"
        }
    }
}

// MARK: - Обработка

struct ProcessScreen: View {
    @EnvironmentObject var settings: Settings
    @EnvironmentObject var input: InputSet
    @EnvironmentObject var run: RunState
    @EnvironmentObject var l10n: L10n

    @Binding var screen: Screen
    let onStart: (Bool) -> Void

    @State private var dropping = false
    @State private var showExtra = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                dropZone
                if !input.skipped.isEmpty || shownMode == .layouts { filters }
                stageList
                extraGroup
            }
            .padding(22)
        }
        .safeAreaInset(edge: .bottom) { bottomBar }
        .onAppear { refreshPlan() }
        .onChange(of: input.kind) { _ in refreshPlan() }
        .onChange(of: settings.fields) { _ in refreshPlan() }
    }

    /// Этапы, которые уйдут в прогон: включённые карточки минус те,
    /// что в этом режиме невозможны. То же самое считает start() —
    /// иначе план и прогон разойдутся.
    static func stagesToRun(_ settings: Settings, _ mode: InputKind) -> [String] {
        let chosen = StageInfo.all
            .filter { mode != .images || !$0.layoutOnly }
            .filter { settings.field("stage.\($0.id)")?.isOn ?? false }
            .map(\.id)
        // «links» в интерфейсе нет: в режиме вёрстки он обязателен (без него
        // неоткуда взять список картинок), в режиме картинок невозможен.
        return mode == .images ? chosen : ["links"] + chosen
    }

    private func refreshPlan() {
        let wanted = ProcessScreen.stagesToRun(settings, input.kind)
        guard wanted != settings.stagesForRun else { return }
        settings.stagesForRun = wanted
        settings.reload()
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 3) {
                Text(l10n["main_title"]).font(.title2).bold()
                Text(shownMode == .images ? l10n["main_hint_images"]
                                           : l10n["main_hint_layout"])
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 16)
            VStack(alignment: .trailing, spacing: 8) {
                enginePicker
                modePicker
            }
        }
    }

    /// Движок — главный выбор на этом экране: от него зависит всё
    /// остальное. Значение живёт в config.json, пишется через пайплайн.
    @ViewBuilder private var enginePicker: some View {
        if let f = settings.field("engine") {
            Picker("", selection: Binding(
                get: { f.options.first { $0.1 == f.value }?.0 ?? "native" },
                set: { settings.set("engine", $0) })) {
                    ForEach(f.options, id: \.0) { v, title in
                        Text(shortEngine(v, title)).tag(v)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 240)
        }
    }

    /// Подписи движка в config подробные («вариант 1: всё на Adobe») —
    /// в сегментах они не помещаются, поэтому короткие.
    private func shortEngine(_ value: String, _ full: String) -> String {
        switch value {
        case "photoshop": return "Photoshop"
        case "native":    return l10n.code == "ru" ? "Apple + ИИ" : "Apple + AI"
        default:          return full
        }
    }

    /// Чем работаем — выбор человека, а не вывод из содержимого папки.
    /// Папка вёрстки всегда содержит и .indd, и картинки в Links, поэтому
    /// «угадывать» тут нечего: надо спросить.
    private var modePicker: some View {
        Picker("", selection: Binding(
            get: { input.kind },
            set: { input.choose($0) })) {
            Text(l10n["mode_layout"]).tag(InputKind.layouts)
            Text(l10n["mode_images"]).tag(InputKind.images)
        }
        .pickerStyle(.segmented).labelsHidden().frame(width: 180)
    }

    private var dropZone: some View {
        VStack(alignment: .leading, spacing: 7) {
            if input.accepted.isEmpty {
                HStack {
                    Image(systemName: "tray.and.arrow.down")
                        .foregroundStyle(.tertiary)
                    Text(l10n["drop_empty"]).foregroundStyle(.tertiary)
                    Spacer()
                    Button(l10n["drop_choose"]) { choose() }
                        .controlSize(.small)
                }
            } else {
                ForEach(input.accepted.prefix(6)) { item in
                    HStack(spacing: 9) {
                        Image(systemName: item.isLayout ? "doc.richtext.fill" : "photo.fill")
                            .foregroundStyle(item.isLayout ? .pink : .accentColor)
                        Text(item.name).lineLimit(1).truncationMode(.middle)
                        if let v = item.indesignVersion {
                            Text("InDesign \(v)")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                }
                if input.accepted.count > 6 {
                    Text(l10n.t("and_more", input.accepted.count - 6))
                        .font(.caption).foregroundStyle(.secondary)
                }
                HStack {
                    Spacer()
                    Button(l10n["drop_choose"]) { choose() }.controlSize(.small)
                }
            }
            // Файлы пришли, но ни один не подошёл. Без этой строки список
            // просто остаётся пустым, и непонятно, что чинить: то ли файл
            // не дошёл, то ли режим не тот, то ли расширение не из списка.
            // Отброшенное как результат прошлых прогонов. Это НЕ ошибка
            // и не фильтр: папки originals/ и prepress_logs/ пропускаются
            // намеренно, иначе повторный проход жевал бы собственные
            // результаты. Но сказать об этом надо — иначе пустой список
            // читается как зависание.
            if input.excluded > 0 {
                Text(l10n.t("skipped_leftovers", input.excluded))
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if input.accepted.isEmpty && input.rawCount > 0 {
                Text(l10n.t("none_matched", input.rawCount))
                    .font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if input.otherCount > 0 {
                // Не предупреждение, а подсказка: в наборе есть файлы
                // другого рода, и переключатель их покажет.
                Text(input.kind == .layouts
                     ? l10n.t("also_images", input.otherCount)
                     : l10n.t("also_layouts", input.otherCount))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(15)
        .background(RoundedRectangle(cornerRadius: 10)
            .fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(
            dropping ? Color.accentColor : Color(nsColor: .separatorColor),
            style: StrokeStyle(lineWidth: dropping ? 2 : 1,
                               dash: input.accepted.isEmpty ? [6, 4] : [])))
        .onDrop(of: [.fileURL], isTargeted: $dropping) { providers in
            Task { await accept(providers) }
            return true
        }
    }

    private var filters: some View {
        HStack(spacing: 16) {
            HStack(spacing: 6) {
                Text(l10n["filter_version"]).foregroundStyle(.secondary)
                Picker("", selection: $input.versionFilter) {
                    Text(l10n["filter_any"]).tag("")
                    ForEach(["2026", "2025", "2024", "2023"], id: \.self) { Text($0).tag($0) }
                }.labelsHidden().frame(width: 96)
            }
            HStack(spacing: 6) {
                Text(l10n["filter_suffix"]).foregroundStyle(.secondary)
                TextField("", text: $input.suffixFilter)
                    .frame(width: 110).font(.callout.monospaced())
            }
            if !input.skipped.isEmpty {
                Text(l10n.t("skipped_n", input.skipped.count))
                    .font(.callout).foregroundStyle(.orange)
            }
            Spacer()
        }
        .font(.callout)
    }

    private var shownMode: InputKind { input.kind }

    private var visibleStages: [StageInfo] {
        StageInfo.primaryStages.filter { shownMode != .images || !$0.layoutOnly }
    }

    private var stageList: some View {
        VStack(spacing: 10) {
            ForEach(Array(visibleStages.enumerated()), id: \.element.id) { i, s in
                StageCard(stage: s, step: i + 1, compact: false)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .animation(.easeOut(duration: 0.25), value: shownMode == .images)
    }

    private var extraGroup: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.easeOut(duration: 0.22)) { showExtra.toggle() }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.bold))
                        .rotationEffect(.degrees(showExtra ? 90 : 0))
                    Text(l10n["stages_more"]).font(.caption.weight(.semibold))
                        .textCase(.uppercase).kerning(0.6)
                }
                .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)

            if showExtra {
                VStack(spacing: 8) {
                    ForEach(StageInfo.extraStages.filter {
                        shownMode != .images || !$0.layoutOnly
                    }) { s in
                        StageCard(stage: s, step: nil, compact: true)
                    }
                }
            }
        }
    }

    private var bottomBar: some View {
        HStack(spacing: 12) {
            // Пересказа этапов тут нет намеренно: он повторял то, что уже
            // написано на карточках, и не помещался в строку. Полезно здесь
            // только то, чего на карточках НЕ видно, — сколько файлов возьмут.
            if !input.accepted.isEmpty {
                Text(l10n.t("will_take", input.accepted.count))
                    .font(.callout).foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)
            Button(l10n["btn_check"]) { onStart(true) }
                .disabled(input.accepted.isEmpty)
            Button(l10n["btn_run"]) { onStart(false) }
                .keyboardShortcut(.defaultAction)
                .disabled(input.accepted.isEmpty)
        }
        .padding(.horizontal, 22).padding(.vertical, 14)
        .background(.bar)
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        if panel.runModal() == .OK { input.accept(panel.urls) }
    }

    private func accept(_ providers: [NSItemProvider]) async {
        var urls: [URL] = []
        for p in providers {
            guard let item = try? await p.loadItem(
                forTypeIdentifier: UTType.fileURL.identifier) else { continue }
            if let data = item as? Data,
               let url = URL(dataRepresentation: data, relativeTo: nil) {
                urls.append(url)
            } else if let url = item as? URL {
                urls.append(url)
            }
        }
        let final = urls
        await MainActor.run { input.accept(final) }
    }
}

// MARK: - Прогон

struct RunScreen: View {
    @EnvironmentObject var run: RunState
    @EnvironmentObject var l10n: L10n
    let onStop: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(l10n["run_title"]).font(.title2).bold()
                    Text(run.document).foregroundStyle(.secondary)
                }
                Spacer()
                if run.isRunning { Button(l10n["btn_stop"], action: onStop) }
            }

            if run.imageTotal > 0 {
                VStack(alignment: .leading, spacing: 6) {
                    ProgressView(value: run.progress)
                    HStack {
                        Text(l10n.t("run_of", run.imageIndex, run.imageTotal))
                            .monospacedDigit()
                        Text("·").foregroundStyle(.tertiary)
                        Text(run.imageName).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        if !run.stage.isEmpty {
                            Text(run.stage).foregroundStyle(.secondary)
                        }
                    }
                    .font(.callout)
                }
            } else if run.isRunning {
                ProgressView().controlSize(.small)
            }

            Text(l10n["log_title"]).font(.caption).foregroundStyle(.secondary)
            LogView(lines: run.logTail)

            if let f = run.failure {
                Label(f, systemImage: "xmark.octagon.fill")
                    .foregroundStyle(.red).font(.callout)
            }
        }
        .padding(22)
    }
}

/// Журнал. Прокрутка следует за концом, пока прогон идёт.
struct LogView: View {
    let lines: [String]

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { i, line in
                        Text(line)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(colour(for: line))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .id(i)
                    }
                }
                .padding(10)
            }
            .background(RoundedRectangle(cornerRadius: 8)
                .fill(Color(nsColor: .textBackgroundColor)))
            .onChange(of: lines.count) { n in
                withAnimation { proxy.scrollTo(n - 1, anchor: .bottom) }
            }
        }
    }

    /// Предупреждения и ошибки видно сразу: в журнале прогона на сотню
    /// картинок одна строка «! …» иначе теряется в тысяче обычных.
    private func colour(for line: String) -> Color {
        if line.contains("ERROR") || line.contains("FAILED") { return .red }
        if line.contains(" ! ") || line.hasPrefix("WARNING") { return .orange }
        return .primary
    }
}

// MARK: - Отчёт

struct ReportScreen: View {
    @EnvironmentObject var run: RunState
    @EnvironmentObject var l10n: L10n

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(l10n["report_title"]).font(.title2).bold()
                Spacer()
                if let path = run.finished?.reportHtml {
                    Button(l10n["report_open_finder"]) {
                        NSWorkspace.shared.activateFileViewerSelecting(
                            [URL(fileURLWithPath: path)])
                    }
                    Button(l10n["btn_report"]) {
                        NSWorkspace.shared.open(URL(fileURLWithPath: path))
                    }
                }
            }
            .padding(22)

            if let path = run.finished?.reportHtml {
                // Отчёт рисует пайплайн, приложение только показывает:
                // одна реализация на терминал, приложение и заказчика.
                ReportWeb(url: URL(fileURLWithPath: path))
            } else {
                Spacer()
                Text(l10n["report_none"]).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
                Spacer()
            }
        }
    }
}

struct ReportWeb: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> WKWebView {
        let v = WKWebView()
        v.setValue(false, forKey: "drawsBackground")
        return v
    }

    func updateNSView(_ v: WKWebView, context: Context) {
        // Доступ к папке отчёта нужен: страница самодостаточна, но WebKit
        // всё равно требует явного разрешения на локальный файл.
        v.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
    }
}

// MARK: - Настройки

struct SettingsScreen: View {
    @EnvironmentObject var settings: Settings
    @EnvironmentObject var l10n: L10n

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(l10n["set_title"]).font(.title2).bold()

                // Язык интерфейса — единственная настройка приложения,
                // а не пайплайна: остальное живёт в config.json.
                GroupBox {
                    HStack {
                        Text(l10n["set_language"])
                        Spacer()
                        Picker("", selection: Binding(
                            get: { l10n.choice },
                            set: { l10n.choice = $0
                                   settings.lang = l10n.code
                                   settings.reload() })) {
                            ForEach(Lang.allCases) { Text($0.title(l10n.code)).tag($0) }
                        }
                        .labelsHidden().frame(width: 200)
                    }
                    .padding(6)
                }

                // Здесь только то, чего НЕТ на экране «Обработка»: остальное
                // человек правит там же, где видит этап, которому оно
                // принадлежит. Раздел без своих настроек не показывается
                // вовсе — пустая рамка ничего не сообщает.
                ForEach(settings.sections, id: \.self) { section in
                    let rows = own(section)
                    if !rows.isEmpty {
                        GroupBox(section) {
                            VStack(spacing: 10) {
                                ForEach(rows) { f in
                                    SettingRow(field: f)
                                    if f.id != rows.last?.id { Divider() }
                                }
                            }
                            .padding(6)
                        }
                    }
                }

                if let err = settings.lastError {
                    Label(err, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange).font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(22)
            .frame(maxWidth: 760, alignment: .leading)
        }
    }

    /// Настройки СОСЕДНИХ инструментов. Конфиг у проекта общий, и
    /// --config-menu честно отдаёт всё, что в нём есть, — но экспорт в PDF
    /// делает export_pdf.py, которого в приложении пока нет. Показывать
    /// настройку этапа, которого не существует, значит обещать кнопку.
    private static let otherTools = ["pdf_export."]

    /// Настройки раздела, которых нет на экране «Обработка».
    private func own(_ section: String) -> [SettingField] {
        settings.fields(in: section)
            // stage.* — это карточки на «Обработке», там же они и живут.
            .filter { !$0.key.hasPrefix("stage.") && !$0.key.hasPrefix("этап.") }
            .filter { !StageInfo.shownOnProcess.contains($0.key) }
            .filter { k in !SettingsScreen.otherTools.contains { k.key.hasPrefix($0) } }
    }
}
