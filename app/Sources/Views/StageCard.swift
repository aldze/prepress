// Карточка этапа: переключатель, подпись, раскрытие.
//
// Состав цепочки правится через пайплайн (--config-set stage.<имя>), а не
// своей копией: проверку допустимых сочетаний держит Python. Поэтому
// переключатель может ОТКАЗАТЬ — например, обрезка с нормировкой масштаба
// требует переподключения. Отказ показывается объяснением из пайплайна,
// а не глотается молча.

import SwiftUI

/// Этапы в порядке прогона. Порядок не настраивается — он задан в пайплайне
/// и каждая связь в нём оплачена пойманным багом.
struct StageInfo: Identifiable {
    let id: String
    let layoutOnly: Bool
    let primary: Bool     // семь основных из ТЗ против дополнительных
    /// Этап без собственной карточки: его тумблер живёт в деталях другой.
    /// В цепочку прогона он попадает наравне с остальными — список этапов
    /// один, иначе включённый этап не дошёл бы до прогона.
    var inCard: String? = nil

    /// «links» в списке НЕТ намеренно: читать связи из InDesign или нет —
    /// это и есть переключатель «Вёрстка / Картинки». Отдельная галочка
    /// была бы вторым органом на то же действие, причём способным ему
    /// противоречить.
    static let all: [StageInfo] = [
        // Уборка полей идёт ПЕРВОЙ и до сбора связей: забытый на полях кадр
        // иначе попадает в список наравне с настоящими.
        .init(id: "pasteboard", layoutOnly: true, primary: true),
        .init(id: "bleed",    layoutOnly: true,  primary: false),
        .init(id: "text",     layoutOnly: true,  primary: false),
        .init(id: "spots",    layoutOnly: true,  primary: false),
        .init(id: "crop",     layoutOnly: true,  primary: true),
        // ИИ-апскейл показан в «resample»: это продолжение той же
        // лестницы разрешений, а не отдельная работа.
        .init(id: "upscale",  layoutOnly: false, primary: false,
              inCard: "resample"),
        .init(id: "lift",     layoutOnly: false, primary: true),
        .init(id: "tone",     layoutOnly: false, primary: true),
        .init(id: "memory",   layoutOnly: false, primary: true),
        .init(id: "master",   layoutOnly: false, primary: false),
        .init(id: "convert",  layoutOnly: false, primary: true),
        .init(id: "resample", layoutOnly: true,  primary: true),
        .init(id: "sharpen",  layoutOnly: false, primary: true),
        .init(id: "relink",   layoutOnly: true,  primary: true),
    ]

    /// Настройки каждого этапа: их видно прямо в карточке, чтобы не гонять
    /// человека между экранами ради одного числа. Внутри карточки НЕТ
    /// настройки, повторяющей её собственный переключатель: два органа
    /// на одно действие — это вопрос «какой из них главный», на который
    /// нет ответа.
    static let settingKeys: [String: [String]] = [
        "crop": ["crop_to_frame.margin_mm"],
        // Порог ИИ-апскейла стоит здесь, а не в карточке «upscale»:
        // все четыре числа — одна лестница разрешений (уменьшить выше 350,
        // цель 300, увеличить ниже 290, ниже 240 интерполяция уже не
        // спасает и зовётся Real-ESRGAN), и читать её надо подряд.
        "resample": ["max_effective_ppi", "target_effective_ppi",
                     "upscale_below_ppi", "stage.upscale", "upscale_min_ppi"],
        "lift": ["lift.mode", "lift.skin_target", "lift.max_exposure", "lift.max_saturation"],
        "tone": ["global_tone.mode", "global_tone.points", "global_tone.engine"],
        "memory": ["memory_colors.mode", "memory_colors.engine",
                   "memory_colors.max_shift_percent",
                   "memory_colors.skin.enabled", "memory_colors.sky.enabled",
                   "memory_colors.grass.enabled"],
        "convert": ["color_engine", "target_icc", "grayscale_icc",
                    "convert_grayscale_to_cmyk"],
        "sharpen": ["sharpen.unsharp"],
        // Масштаб 100% стоит здесь, а не в «crop»: обрезке он не подчинён.
        // plan_crop считает план и когда резать нечего — ровно ради него, —
        // а ставит масштаб relink.jsx. В карточке обрезки строка обещала
        // зависимость, которой в пайплайне нет.
        "relink": ["crop_to_frame.scale_to_100",
                   "output_document.save_as_new", "output_document.suffix"],
        "bleed": ["bleed_required_mm", "bleed_scope"],
        "text": ["text_in_links.mode"],
        "spots": ["spot_colors.mode"],
    ]

    /// Что человек уже видит на экране «Обработка». Экран настроек это
    /// не показывает: одна и та же ручка на двух экранах расходится
    /// в голове раньше, чем в конфиге.
    static let shownOnProcess: Set<String> = {
        var keys = Set(settingKeys.values.joined())
        keys.formUnion([
            "engine",       // сегменты «Photoshop / Apple + AI» в шапке
            "pipeline",     // переключатель «Вёрстка / Картинки»
            // Язык отчёта равен языку интерфейса и уходит в прогон
            // флагом --lang, см. PrepressApp.start.
            "report_lang",
            // Прежние выключатели тех же этапов: карточка уже несёт свой,
            // а пайплайн двигает эти флаги вместе с составом цепочки.
            "crop_to_frame.enabled", "ai_upscale.enabled",
            "master.enabled", "sharpen.enabled",
        ])
        return keys
    }()

    static var primaryStages: [StageInfo] {
        all.filter { $0.primary && $0.inCard == nil }
    }
    static var extraStages: [StageInfo] {
        all.filter { !$0.primary && $0.inCard == nil }
    }
}

struct StageCard: View {
    let stage: StageInfo
    let step: Int?
    let compact: Bool

    @EnvironmentObject var settings: Settings
    @EnvironmentObject var l10n: L10n
    @State private var open = false

    private var field: SettingField? { settings.field("stage.\(stage.id)") }
    private var isOn: Bool { field?.isOn ?? false }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 14) {
                Toggle("", isOn: Binding(
                    get: { isOn },
                    set: { settings.toggleStage(stage.id, on: $0) }))
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .controlSize(.small)

                VStack(alignment: .leading, spacing: 2) {
                    Text(field?.label ?? stage.id)
                        .font(compact ? .body : .title3.weight(.semibold))
                        .foregroundStyle(.primary)
                    if !compact, let sub = subtitle {
                        Text(sub).font(.callout).foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                // Пометки «только вёрстка» здесь НЕТ намеренно: в режиме
                // картинок эти карточки не показываются вовсе, а в режиме
                // вёрстки пометка сообщает о невозможном положении дел.
                if let step, !compact {
                    Text(String(format: "%02d", step))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.tertiary)
                }
                if !related.isEmpty {
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(open ? 90 : 0))
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, compact ? 10 : 14)
            .opacity(isOn ? 1 : 0.55)
            // Нажимается ВСЯ строка, а не только заголовок: маленькая
            // мишень у стрелки — это промах на промахе. contentShape
            // обязателен, иначе пустое место в строке не ловит нажатие.
            .contentShape(Rectangle())
            .onTapGesture {
                guard !related.isEmpty else { return }
                withAnimation(.easeOut(duration: 0.22)) { open.toggle() }
            }

            if open, !related.isEmpty {
                Divider().padding(.leading, 60)
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(related) { f in
                        // «Выключено» из трёхступенчатых меню убрано:
                        // этап выключается тумблером слева, и второй
                        // способ сделать то же самое только путал бы.
                        SettingRow(field: f, compact: true, hidesOff: true)
                    }
                }
                .padding(.leading, 60).padding(.trailing, 16)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background(RoundedRectangle(cornerRadius: 10)
            .fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 10)
            .strokeBorder(Color(nsColor: .separatorColor)))
    }

    /// Подпись под названием — первая строка плана, относящаяся к этапу.
    private var subtitle: String? { nil }

    /// Настройки, относящиеся к этому этапу: показываем их прямо в карточке,
    /// чтобы не гонять человека между экранами ради одного числа.
    /// Внутри карточки НЕТ настройки, повторяющей её собственный
    /// переключатель: «Sharpening» внутри «Apply sharpening», «Keep a master»
    /// внутри себя же. Два органа на одно действие — это не гибкость,
    /// а вопрос «какой из них главный», на который нет ответа.
    private var related: [SettingField] {
        (StageInfo.settingKeys[stage.id] ?? []).compactMap { settings.field($0) }
    }
}


// MARK: - Строка настройки

/// Одна настройка. Вид зависит от типа, который сообщил пайплайн:
/// bool, choice, number, file, mode, text.
struct SettingRow: View {
    let field: SettingField
    var compact = false
    /// Убрать вариант «выключено»: внутри карточки этапа его роль
    /// исполняет тумблер.
    var hidesOff = false

    @EnvironmentObject var settings: Settings
    @State private var text: String = ""

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(field.label)
                .frame(maxWidth: compact ? 240 : .infinity, alignment: .leading)
                .foregroundStyle(compact ? .secondary : .primary)
            Spacer(minLength: 8)
            control
        }
    }

    @ViewBuilder private var control: some View {
        switch field.kind {
        case "bool":
            Toggle("", isOn: Binding(
                get: { field.isOn },
                set: { settings.set(field.key, $0 ? "да" : "нет") }))
                .labelsHidden().toggleStyle(.switch).controlSize(.small)

        case "choice", "mode":
            Picker("", selection: Binding(
                get: { current },
                set: { settings.set(field.key, $0) })) {
                    ForEach(options, id: \.0) { v, title in
                        Text(title).tag(v)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .frame(maxWidth: 260)

        case "number":
            TextField("", text: $text)
                .frame(width: 80)
                .multilineTextAlignment(.trailing)
                .onSubmit { settings.set(field.key, text) }
                .onAppear { text = field.value }
                .onChange(of: field.value) { text = $0 }

        case "file":
            HStack(spacing: 6) {
                Text(field.value).font(.caption.monospaced())
                    .lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(.secondary)
                Button("…") { pickFile() }.controlSize(.small)
            }
            .frame(maxWidth: 320, alignment: .trailing)

        default:
            TextField("", text: $text)
                .frame(width: 140)
                .onSubmit { settings.set(field.key, text) }
                .onAppear { text = field.value }
                .onChange(of: field.value) { text = $0 }
        }
    }

    private var options: [(String, String)] {
        guard hidesOff, field.kind == "mode" else { return field.options }
        return field.options.filter { $0.0 != "off" }
    }

    /// Значение выбора приходит подписью, а сохранять надо код: ищем код
    /// по подписи. Если подпись не нашлась (перевод разошёлся) — берём
    /// первый вариант, чтобы меню не оказалось пустым.
    private var current: String {
        field.options.first { $0.1 == field.value }?.0
            ?? options.first?.0 ?? field.value
    }

    private func pickFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.init(filenameExtension: "icc")].compactMap { $0 }
        if panel.runModal() == .OK, let url = panel.url {
            settings.set(field.key, url.path)
        }
    }
}
