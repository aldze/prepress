// Prepress.app — оболочка над пайплайном.
//
// Приложение не обрабатывает изображения само: оно запускает prepress.py
// и показывает, что тот делает. Вся логика препресса, все инварианты
// и все замеры остаются в Python — одна реализация на терминал,
// приложение и тесты.

import SwiftUI

@main
struct PrepressApp: App {
    @StateObject private var settings = Settings()
    @StateObject private var input = InputSet()
    @StateObject private var run = RunState()
    @StateObject private var l10n = L10n()

    var body: some Scene {
        WindowGroup("Prepress") {
            RootView()
                .environmentObject(settings)
                .environmentObject(input)
                .environmentObject(run)
                .environmentObject(l10n)
                .frame(minWidth: 900, minHeight: 640)
                .onAppear {
                    settings.lang = l10n.code
                    settings.reload()
                }
        }
        .commands { CommandGroup(replacing: .newItem) { } }
    }
}

struct RootView: View {
    @EnvironmentObject var settings: Settings
    @EnvironmentObject var input: InputSet
    @EnvironmentObject var run: RunState
    @EnvironmentObject var l10n: L10n

    @State private var screen: Screen = .process
    @State private var runner: PipelineRun?

    var body: some View {
        NavigationSplitView {
            List(selection: $screen) {
                Section(l10n["nav_group_work"]) {
                    ForEach([Screen.process, .run, .report]) { s in
                        Label(l10n[s.titleKey], systemImage: s.icon).tag(s)
                    }
                }
                Section(l10n["nav_group_app"]) {
                    ForEach([Screen.diag, .settings]) { s in
                        Label(l10n[s.titleKey], systemImage: s.icon).tag(s)
                    }
                }
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 190, ideal: 208, max: 240)
        } detail: {
            switch screen {
            case .process:
                ProcessScreen(screen: $screen, onStart: start)
            case .run:
                RunScreen(onStop: { runner?.cancel() })
            case .report:
                ReportScreen()
            case .diag:
                DiagnosticsScreen()
            case .settings:
                SettingsScreen()
            }
        }
        .onChange(of: run.finished?.reportHtml) { path in
            // Отчёт открывается сам по окончании прогона: это последнее,
            // ради чего запускали, и искать его по меню незачем.
            if path != nil { screen = .report }
        }
    }

    private func start(dry: Bool) {
        run.reset()
        screen = .run
        let r = PipelineRun()
        runner = r
        // Язык передаём явно: config.json не знает, на каком языке
        // приложение говорит с человеком, а отчёт читает человек.
        var args: [String] = ["--lang", l10n.code]
        // Цепочку собираем ИЗ ТОГО, ЧТО НА ЭКРАНЕ: включённые карточки минус
        // этапы, невозможные в этом режиме. Иначе выходит разрыв — человек
        // выбрал «картинки», а цепочка осталась вёрсточной, и пайплайн
        // отказывает за то, чего пользователь не выбирал.
        let stages = ProcessScreen.stagesToRun(settings, input.kind)
        if !stages.isEmpty { args += ["--stages", stages.joined(separator: ",")] }
        if dry { args.append("--dry-run") }
        args += input.accepted.map { $0.url.path }
        r.start(arguments: args,
                onEvent: { run.apply($0) },
                onLog: { run.appendLog($0) },
                onFinish: { result in
                    run.isRunning = false
                    if case .failure(let e) = result {
                        run.failure = e.localizedDescription
                    }
                })
    }
}
