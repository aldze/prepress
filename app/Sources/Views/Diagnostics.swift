// Диагностика: что стоит на машине и чего не хватает.
//
// Это экранная версия того, что делает setup.sh. Приложение не чинит
// окружение молча: оно показывает, чего нет, и предлагает поставить —
// решает человек. ImageMagick ставится в фоне через Homebrew, как просили
// в ТЗ; сам Homebrew поставить из приложения нельзя (его установщик просит
// пароль администратора), поэтому без него — только подсказка.

import SwiftUI

struct Check: Identifiable {
    enum State { case ok, warn, missing }
    let id: String
    let title: String
    let detail: String
    var state: State
    var installable: Bool = false
}

@MainActor
final class Diagnostics: ObservableObject {
    @Published private(set) var checks: [Check] = []
    @Published var installing: String?
    @Published var installLog: String = ""

    private let paths = PipelinePaths.locate()

    func refresh() {
        var out: [Check] = []
        let fm = FileManager.default

        let brew = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
            .first { fm.isExecutableFile(atPath: $0) }
        let magick = ["/opt/homebrew/bin/magick", "/usr/local/bin/magick"]
            .first { fm.isExecutableFile(atPath: $0) }

        out.append(Check(id: "magick", title: "ImageMagick",
                         detail: magick ?? "brew install imagemagick",
                         state: magick != nil ? .ok : .missing,
                         installable: magick == nil && brew != nil))
        out.append(Check(id: "brew", title: "Homebrew",
                         detail: brew ?? "https://brew.sh",
                         state: brew != nil ? .ok : .warn))

        out.append(Check(id: "python", title: "Python",
                         detail: paths.python.path,
                         state: fm.isExecutableFile(atPath: paths.python.path) ? .ok : .missing))
        out.append(Check(id: "pipeline", title: "prepress.py",
                         detail: paths.script.path,
                         state: fm.fileExists(atPath: paths.script.path) ? .ok : .missing))

        // Помощники Apple и модель разбора человека.
        for (name, title) in [("vtone", "Core Image (vtone)"),
                              ("vmask", "Vision (vmask)"),
                              ("vskin", "Разбор человека (vskin)")] {
            let p = paths.toolsDir.appendingPathComponent(name).path
            out.append(Check(id: name, title: title, detail: p,
                             state: fm.isExecutableFile(atPath: p) ? .ok : .warn))
        }
        let model = paths.toolsDir
            .appendingPathComponent("models/selfie_multiclass.mlpackage").path
        out.append(Check(id: "model", title: "Модель разбора человека",
                         detail: model,
                         state: fm.fileExists(atPath: model) ? .ok : .warn))

        for (app, title) in [("Adobe InDesign 2026", "Adobe InDesign"),
                             ("Adobe Photoshop 2026", "Adobe Photoshop"),
                             ("Adobe Illustrator 2026", "Adobe Illustrator")] {
            let p = "/Applications/\(app)"
            out.append(Check(id: app, title: title, detail: p,
                             state: fm.fileExists(atPath: p) ? .ok : .warn))
        }

        checks = out
    }

    /// Установка ImageMagick в фоне. Вывод brew идёт в журнал: установка
    /// длится минуты, и «крутилка без подробностей» здесь бесполезна.
    func install(_ id: String) {
        guard id == "magick" else { return }
        let brew = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
        guard let brew else { return }
        installing = id
        installLog = ""

        let p = Process()
        p.executableURL = URL(fileURLWithPath: brew)
        p.arguments = ["install", "imagemagick"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { fh in
            guard let s = String(data: fh.availableData, encoding: .utf8), !s.isEmpty
            else { return }
            Task { @MainActor [weak self] in
                self?.installLog += s
                if let n = self?.installLog.count, n > 8000 {
                    self?.installLog.removeFirst(n - 8000)
                }
            }
        }
        p.terminationHandler = { _ in
            Task { @MainActor [weak self] in
                self?.installing = nil
                self?.refresh()
            }
        }
        try? p.run()
    }
}

struct DiagnosticsScreen: View {
    @EnvironmentObject var l10n: L10n
    @StateObject private var diag = Diagnostics()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(l10n["diag_title"]).font(.title2).bold()
                        Text(l10n["diag_hint"]).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(l10n["btn_recheck"]) { diag.refresh() }
                }

                ForEach(diag.checks) { c in
                    HStack(spacing: 12) {
                        Circle().fill(colour(c.state)).frame(width: 9, height: 9)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(c.title).fontWeight(.medium)
                            Text(c.detail).font(.caption.monospaced())
                                .foregroundStyle(.tertiary)
                                .lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                        if c.installable {
                            if diag.installing == c.id {
                                HStack(spacing: 6) {
                                    ProgressView().controlSize(.small)
                                    Text(l10n["diag_installing"]).font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            } else {
                                Button(l10n["btn_install"]) { diag.install(c.id) }
                                    .controlSize(.small)
                            }
                        } else {
                            Text(c.state == .ok ? l10n["diag_ok"]
                                 : c.state == .missing ? l10n["diag_missing"] : "!")
                                .font(.caption)
                                .foregroundStyle(c.state == .ok ? .green
                                                 : c.state == .missing ? .red : .orange)
                        }
                    }
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: 9)
                        .fill(Color(nsColor: .controlBackgroundColor)))
                    .overlay(RoundedRectangle(cornerRadius: 9)
                        .strokeBorder(Color(nsColor: .separatorColor)))
                }

                if !diag.installLog.isEmpty {
                    Text(diag.installLog)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 8)
                            .fill(Color(nsColor: .textBackgroundColor)))
                }
            }
            .padding(22)
            .frame(maxWidth: 820, alignment: .leading)
        }
        .onAppear { diag.refresh() }
    }

    private func colour(_ s: Check.State) -> Color {
        switch s {
        case .ok: return .green
        case .warn: return .orange
        case .missing: return .red
        }
    }
}
