(*
	PressPDF.app — приложение-дроплет для постраничного экспорта вёрстки в PDF.

	Как пользоваться:
	  • перетащить .indd (или папку с макетами) на иконку в Dock;
	  • либо запустить двойным кликом — предложит выбрать файлы.

	Каждая полоса уходит в отдельный PDF с именем «<номер><код выпуска>.pdf»
	(038_Y123.pdf), пресет экспорта — «[Press Quality]». Готовое складывается
	в папку PressPDF на уровень выше папки макета, где собирается весь номер.

	Из брошенной ПАПКИ берутся только макеты с суффиксом _CMYK: обычный .indd
	рядом с ними — исходник со связями на RGB-картинки, и печатный PDF из него
	был бы браком. Брошенный ФАЙЛ берётся какой дали. Правило это живёт
	в export_pdf.py (--list), здесь оно не продублировано намеренно: два места
	с одним правилом рано или поздно расходятся.

	Пересборка после правок:
	    droplet/build_presspdf.sh

	------------------------------------------------------------------------
	Пять правил, которыми продиктовано устройство этого файла. Они те же, что
	в Prepress.applescript, и по той же причине: каждое — вылеченный баг,
	а не осторожность впрок. Здесь они повторены, потому что приложение
	отдельное и подсказать их больше некому.

	1. ВЫЙТИ ВПЕРЁД ДО ВСЕГО ОСТАЛЬНОГО.
	   Приложение, запущенное броском файла на иконку, на передний план не
	   выходит. Окно, показанное в таком состоянии, открывается позади чужих:
	   кликнуть по нему нельзя, иконка в Dock прыгает, а человек видит «мяч».
	   Поэтому activate — самое первое действие обоих входов.
	   Замер (Prepress): без activate окно не выходит вперёд и за 35 с; с ним — 2.8 с.

	2. НИКАКОГО SYSTEM EVENTS.
	   Любое обращение к нему — Apple Event, требующий отдельного разрешения
	   «Автоматизация». Всё, что здесь нужно, делается своими силами и шеллом.

	3. У ОКОН НЕ ДОЛЖНО БЫТЬ ТАЙМАУТА.
	   Окно показывается Apple Event'ом, а на него по умолчанию отведено
	   120 секунд. Если человек думает дольше, окно молча умирает, а скрипт
	   падает с -1712. Поэтому каждое окно обёрнуто в with timeout.

	4. ДОЛГАЯ РАБОТА — ТОЛЬКО В ФОНЕ.
	   do shell script держит главный поток до конца команды. Пока он держит,
	   приложение не разбирает события, и macOS показывает «мяч». Поэтому
	   export_pdf.py запускается в фон, а ждём его циклом с delay: delay,
	   в отличие от do shell script, event loop крутит. См. runInBackground.

	5. ОДИН ПРОГОН ЗА РАЗ.
	   Прямое следствие правила 4: раз ожидание крутит event loop, апплет
	   принимает новый бросок на иконку прямо посреди идущего прогона. Два
	   параллельных экспорта писали бы в одни и те же файлы PDF. Оба входа
	   проходят через claimRun.

	И одно правило, которого нет в Prepress.applescript, но которое так же
	дорого куплено:

	6. НЕ ЗАВОДИТЬ ПЕРЕМЕННЫХ С ИМЕНАМИ СВОЙСТВ ОБЪЕКТНОЙ МОДЕЛИ —
	   kind, bounds, name, class, id, index, result, size, count, text, path.
	   «set kind to "число"» не создаёт переменную, а пытается присвоить
	   свойство, и падает в рантайме (-10006). osacompile такое пропускает:
	   синтаксис верный. Проверять только исполнением.
*)

property PYTHON_BIN : "/usr/bin/python3"
-- Путь подставляет build_presspdf.sh при сборке — в исходнике его быть
-- не должно, иначе проект не перенести на другую машину.
property EXPORT_PY : "@@EXPORT_PY@@"

property RUN_TIMEOUT : 3600
property DIALOG_TIMEOUT : 86400

-- Как часто опрашивать фоновый прогон (правило 4). Полсекунды — компромисс:
-- индикатор в Dock обновляется живо, а на опрос уходит один короткий
-- /bin/cat дважды в секунду.
property POLL_INTERVAL : 0.5

property MAX_LISTED : 12
property APP_TITLE : "PressPDF"

-- Идёт ли прогон прямо сейчас. Правило 5.
property BUSY : false

-- Журнал шагов: ~/Library/Logs/presspdf_droplet.log
property DEBUG_LOG : true


-- ===========================================================================
--  Входы
-- ===========================================================================

-- Двойной клик по иконке: намерение неочевидно, в отличие от броска файлов,
-- поэтому предлагаем выбрать файлы, а не запускаем что-то само.
on run
	bringForward()
	logStep("--- запуск двойным кликом ---")
	if not claimRun() then return
	try
		set choice to askUser("Перетащите .indd или папку на иконку — " & ¬
			"или выберите действие." & return & return & ¬
			"Каждая полоса уйдёт в отдельный PDF в папке PressPDF.", ¬
			{"Отмена", "Пресет", "Выбрать файлы"}, 3, "note")
		if choice is "Пресет" then
			showPresetSetting()
		else if choice is "Выбрать файлы" then
			if exportPyExists() then
				set chosen to pickFiles()
				if chosen is not {} then handleItems(chosen)
			end if
		end if
	on error errMsg number errNum
		reportFailure(errMsg, errNum)
	end try
	set BUSY to false
end run

on open droppedItems
	bringForward()
	logStep("--- запуск броском на иконку (" & (count of droppedItems) & " объектов) ---")
	if not claimRun() then return
	try
		if exportPyExists() then handleItems(droppedItems)
	on error errMsg number errNum
		reportFailure(errMsg, errNum)
	end try
	set BUSY to false
end open


-- ===========================================================================
--  Защита от повторного входа
-- ===========================================================================

-- Тело обоих входов обёрнуто в try, чтобы флаг BUSY снимался при любом исходе.
-- Но глушить ошибку при этом нельзя: пока она глушилась, любой сбой внутри
-- просто закрывал окно, и снаружи это выглядело как «приложение не работает» —
-- без единой подсказки о причине. Отмену (-128) не показываем: это не сбой.
on reportFailure(errMsg, errNum)
	logStep("СБОЙ: " & errMsg & " (" & errNum & ")")
	if errNum is -128 then return
	try
		tellUser("Не получилось:" & return & return & errMsg & ¬
			return & return & "Код ошибки: " & errNum, "stop")
	end try
end reportFailure

-- Правило 5. Оба входа проходят только отсюда, поэтому второй прогон
-- физически не может стартовать поверх первого.
on claimRun()
	if BUSY then
		logStep("отказ: прогон уже идёт")
		try
			tellUser("Экспорт уже идёт." & return & return & ¬
				"Дождитесь окончания и повторите.", "caution")
		end try
		return false
	end if
	set BUSY to true
	return true
end claimRun


-- ===========================================================================
--  Передний план
-- ===========================================================================

-- Правило 1. activate вызывается дважды с паузой: одиночный вызов в момент
-- запуска может не успеть отработать — приложение ещё поднимается, и запрос
-- на выход вперёд теряется.
on bringForward()
	try
		activate
		delay 0.1
		activate
	end try
end bringForward


-- ===========================================================================
--  Окна
-- ===========================================================================

-- Все окна показываются только отсюда: тут и выход вперёд, и снятый таймаут,
-- так что забыть их в новом месте невозможно.
on askUser(theText, theButtons, defaultIndex, iconKind)
	bringForward()
	logStep("окно: " & theText's paragraph 1)
	with timeout of DIALOG_TIMEOUT seconds
		if iconKind is "stop" then
			set dlg to display dialog theText buttons theButtons default button defaultIndex ¬
				with title APP_TITLE with icon stop
		else if iconKind is "caution" then
			set dlg to display dialog theText buttons theButtons default button defaultIndex ¬
				with title APP_TITLE with icon caution
		else
			set dlg to display dialog theText buttons theButtons default button defaultIndex ¬
				with title APP_TITLE with icon note
		end if
	end timeout
	set answer to button returned of dlg
	logStep("  нажато: " & answer)
	return answer
end askUser

on tellUser(theText, iconKind)
	try
		askUser(theText, {"OK"}, 1, iconKind)
	on error number -128
		-- окно закрыли — сообщать было не о чем
	end try
end tellUser

on pickFiles()
	bringForward()
	logStep("окно выбора файлов")
	try
		with timeout of DIALOG_TIMEOUT seconds
			set chosen to choose file with prompt ¬
				"Выберите файлы вёрстки InDesign (.indd)" with multiple selections allowed
		end timeout
	on error number -128
		logStep("  выбор отменён")
		return {}
	end try
	return chosen
end pickFiles


-- ===========================================================================
--  Журнал
-- ===========================================================================

on nowStamp()
	set d to (current date)
	set h to text -2 thru -1 of ("0" & (hours of d))
	set m to text -2 thru -1 of ("0" & (minutes of d))
	set s to text -2 thru -1 of ("0" & ((d's seconds) as integer))
	return h & ":" & m & ":" & s
end nowStamp

on logStep(msg)
	if not DEBUG_LOG then return
	set fh to missing value
	try
		set p to (POSIX path of (path to library folder from user domain)) & ¬
			"Logs/presspdf_droplet.log"
		set fh to open for access (POSIX file p) with write permission
		-- Без «as utf8» AppleScript пишет в MacRoman и кириллица станет «?».
		write (nowStamp() & "  " & msg & linefeed) to fh starting at eof as «class utf8»
		close access fh
	on error
		if fh is not missing value then
			try
				close access fh
			end try
		end if
	end try
end logStep


-- ===========================================================================
--  Мелочи
-- ===========================================================================

-- Окно индикатора подгоняется под длину текста, поэтому всё, что приходит
-- извне (имена файлов, строки лога), обязано быть обрезано.
on trimTo(t, maxLen)
	set t to t as text
	if (count of t) > maxLen then return (text 1 thru (maxLen - 1) of t) & "…"
	return t
end trimTo

on splitText(t, delim)
	set savedDelims to AppleScript's text item delimiters
	set AppleScript's text item delimiters to delim
	set parts to text items of (t as text)
	set AppleScript's text item delimiters to savedDelims
	return parts
end splitText

on joinText(theList, delim)
	set savedDelims to AppleScript's text item delimiters
	set AppleScript's text item delimiters to delim
	set out to theList as text
	set AppleScript's text item delimiters to savedDelims
	return out
end joinText

on baseName(posixPath)
	return item -1 of my splitText(posixPath, "/")
end baseName

-- Последние maxLines строк вывода: там итог прогона и список того, что
-- не получилось. Целиком вывод в окно не влезет — на номере из сорока полос
-- это под сотню строк, а окно растёт по тексту.
on tailOf(t, maxLines)
	try
		set allLines to paragraphs of (t as text)
		set fromLine to (count of allLines) - maxLines + 1
		if fromLine < 1 then set fromLine to 1
		return my joinText(items fromLine thru -1 of allLines, return)
	on error
		return t as text
	end try
end tailOf

on exportPyExists()
	try
		POSIX file EXPORT_PY as alias
		return true
	end try
	tellUser("Не найден export_pdf.py:" & return & return & EXPORT_PY & return & return & ¬
		"Проект переехал? Пересоберите приложение: droplet/build_presspdf.sh", "stop")
	return false
end exportPyExists


-- ===========================================================================
--  Настройка: пресет экспорта
-- ===========================================================================
--
-- Единственная настройка приложения, поэтому общей панели, как у Prepress,
-- здесь нет — она стоила бы полутора сотен строк ради одной строки значения.
-- Хранится настройка там же, где все остальные (config.json, pdf_export.preset),
-- и её видно в панели настроек Prepress; читать и писать её умеет export_pdf.py.
--
-- Имя пресета должно совпадать с InDesign посимвольно, поэтому предлагаем
-- выбор из установленных, а не ввод руками. Ввод остаётся запасным путём:
-- если InDesign не отвечает, список придёт пустым.

on runPy(argsText)
	return do shell script quoted form of PYTHON_BIN & " " & ¬
		quoted form of EXPORT_PY & " " & argsText
end runPy

on showPresetSetting()
	set current to ""
	set installed to {}
	try
		set res to runPy("--list-presets")
		set allLines to paragraphs of res
		if (count of allLines) > 0 then set current to (item 1 of allLines) as text
		repeat with i from 2 to (count of allLines)
			set one to (item i of allLines) as text
			if one is not "" then set end of installed to one
		end repeat
	on error errMsg number errNum
		reportFailure(errMsg, errNum)
		return
	end try

	set picked to ""
	if (count of installed) > 0 then
		bringForward()
		with timeout of DIALOG_TIMEOUT seconds
			set chosenList to choose from list installed with title APP_TITLE ¬
				with prompt "Пресет экспорта PDF. Сейчас: " & current ¬
				default items {current} OK button name "Выбрать" ¬
				cancel button name "Отмена"
		end timeout
		if chosenList is false then return
		if chosenList is {} then return
		set picked to (item 1 of chosenList) as text
	else
		-- InDesign не ответил: список пуст, остаётся ввод руками.
		bringForward()
		with timeout of DIALOG_TIMEOUT seconds
			set dlg to display dialog ¬
				"InDesign не ответил, список пресетов получить не удалось." & return & return & ¬
				"Введите имя пресета точно так, как оно названо в InDesign:" ¬
				default answer current with title APP_TITLE buttons {"Отмена", "Сохранить"} ¬
				default button 2 with icon caution
		end timeout
		if button returned of dlg is "Отмена" then return
		set picked to text returned of dlg
	end if

	try
		set saved to runPy("--set-preset " & quoted form of picked)
		logStep("пресет: " & picked)
		tellUser(saved, "note")
	on error errMsg number errNum
		reportFailure(errMsg, errNum)
	end try
end showPresetSetting


-- ===========================================================================
--  Основной сценарий
-- ===========================================================================

-- Список макетов спрашиваем у самого export_pdf.py (--list): правило «в папке
-- только _CMYK» описано там, и повторять его здесь на find значило бы завести
-- второе такое правило, которое однажды разойдётся с первым.
on resolveLayouts(theItems)
	set argsText to ""
	repeat with itm in theItems
		set argsText to argsText & " " & quoted form of (POSIX path of itm)
	end repeat
	set found to {}
	try
		set res to do shell script quoted form of PYTHON_BIN & " " & ¬
			quoted form of EXPORT_PY & " --list" & argsText
		repeat with ln in paragraphs of res
			set s to ln as text
			if s is not "" then set end of found to s
		end repeat
	on error errMsg number errNum
		-- Код возврата 1 означает «ничего не нашлось» — это не сбой,
		-- о нём скажет handleItems. Всё прочее показываем.
		if errNum is not 1 then error errMsg number errNum
	end try
	logStep("найдено макетов: " & (count of found))
	return found
end resolveLayouts

on handleItems(theItems)
	set layouts to resolveLayouts(theItems)
	set n to count of layouts
	if n = 0 then
		tellUser("Не нашёл ни одного макета для экспорта." & return & return & ¬
			"Бросьте файл .indd — или папку, в которой есть макеты " & ¬
			"с суффиксом _CMYK.", "caution")
		return
	end if

	set listText to ""
	set shown to 0
	repeat with p in layouts
		if shown < MAX_LISTED then
			set listText to listText & "• " & baseName(p) & return
			set shown to shown + 1
		end if
	end repeat
	if n > shown then set listText to listText & "… и ещё " & (n - shown) & return

	-- «Только показать» перед экспортом не роскошь: имена PDF собираются
	-- из кода выпуска и номеров полос, и увидеть их до записи дешевле, чем
	-- разбирать потом сорок неверно названных файлов.
	try
		set choice to askUser("Макетов: " & n & return & return & listText & return & ¬
			"Каждая полоса — отдельный PDF в папке PressPDF.", ¬
			{"Отмена", "Только показать", "Экспорт"}, 3, "note")
	on error number -128
		return
	end try
	if choice is "Отмена" then return

	set flagPart to ""
	if choice is "Только показать" then set flagPart to " --dry-run"

	runExport(layouts, flagPart, choice)
end handleItems

on runExport(layouts, flagPart, choice)
	set n to count of layouts
	set argsText to ""
	repeat with p in layouts
		set argsText to argsText & " " & quoted form of (p as text)
	end repeat

	logStep("экспорт: макетов " & n & ", режим " & choice)

	try
		set progress total steps to 0
		set progress completed steps to 0
		set progress description to "PressPDF: " & trimTo(baseName(item 1 of layouts), 50)
		set progress additional description to "Запуск"
	end try

	-- -u обязателен. Вывод уходит в файл, а не в терминал, поэтому Python
	-- буферизует его блоками по несколько килобайт: без -u индикатор
	-- показывал бы ход работы пачками с большим запозданием.
	set cmd to quoted form of PYTHON_BIN & " -u " & quoted form of EXPORT_PY & ¬
		flagPart & argsText

	set res to runInBackground(cmd)
	set rc to item 1 of res
	set outText to item 2 of res

	try
		set progress total steps to 1
		set progress completed steps to 1
	end try

	-- Хвост вывода — там итог и список того, что не получилось.
	set tailText to tailOf(outText, 12)

	if rc = 0 then
		logStep("  готово")
		if choice is "Только показать" then
			tellUser("Показ без записи. Получилось бы вот что:" & return & return & ¬
				tailText, "note")
		end if
		-- Об успешном экспорте окном не сообщаем: об окончании говорит
		-- системное уведомление, которое шлёт сам export_pdf.py.
	else
		logStep("  ОШИБКА (код " & rc & ")")
		tellUser("Экспорт завершился с ошибками." & return & return & tailText & ¬
			return & return & "Подробности: ~/Library/Logs/presspdf.log", "stop")
	end if
end runExport


-- ===========================================================================
--  Фоновый прогон
-- ===========================================================================

-- Правило 4. Возвращает {код возврата, вывод прогона}.
--
-- Прогон уходит в фон, а ждём его через delay. Разница принципиальная:
-- do shell script держит главный поток до конца команды, и всё это время
-- приложение не разбирает события — macOS считает его повисшим и рисует «мяч».
-- delay, наоборот, крутит event loop: окна перерисовываются, индикатор в Dock
-- живой, приложение отвечает.
--
-- Почему $! пишется в файл: по нему прогон можно убить, если тот перебрал
-- RUN_TIMEOUT. Без этого зависший python остался бы работать в фоне уже
-- после того, как приложение о нём забыло.
on runInBackground(cmd)
	-- Рабочие файлы прогона держим В СВОЁМ каталоге, а не в общем
	-- TemporaryItems. Тот защищён: создать файл в нём можно, а увидеть или
	-- удалить чужой — «Operation not permitted». Достаточно, чтобы кто-то
	-- однажды положил туда файл с тем же именем (хватит и запуска этого же
	-- скрипта не из приложения), и приложение перестаёт работать насовсем:
	-- оно падает на попытке убрать «свой» файл ещё до старта прогона.
	-- Имена вдобавок уникальны, поэтому чистить перед прогоном нечего.
	set runDir to (POSIX path of (path to library folder from user domain)) & ¬
		"Caches/local.presspdf.droplet/"
	do shell script "/bin/mkdir -p " & quoted form of runDir
	set token to do shell script "/usr/bin/uuidgen"
	set outPath to runDir & token & ".out"
	set donePath to runDir & token & ".done"
	set pidPath to runDir & token & ".pid"

	-- Внешние скобки с & — весь конвейер отправляется в фон целиком, поэтому
	-- do shell script возвращается сразу, а не ждёт python.
	do shell script "( " & cmd & " > " & quoted form of outPath & " 2>&1 & " & ¬
		"/bin/echo $! > " & quoted form of pidPath & "; " & ¬
		"wait $!; /bin/echo $? > " & quoted form of donePath & ¬
		" ) > /dev/null 2>&1 &"

	set waited to 0
	repeat
		delay POLL_INTERVAL
		set waited to waited + POLL_INTERVAL

		try
			set rc to (do shell script "/bin/cat " & quoted form of donePath) as integer
			exit repeat
		end try

		if waited > RUN_TIMEOUT then
			try
				do shell script "/bin/kill -9 $(/bin/cat " & quoted form of pidPath & ")"
			end try
			logStep("  прогон убит по таймауту (" & RUN_TIMEOUT & " с)")
			cleanUpRun(outPath, donePath, pidPath)
			return {-1, "Экспорт не уложился в " & RUN_TIMEOUT & " секунд и был остановлен."}
		end if

		-- Ход работы берём только из строк вида «[3/11] имя.indd»: их
		-- export_pdf.py печатает на каждую ПОЛОСУ, и по ним двигается бегунок.
		-- Шагами намеренно сделаны полосы, а не макеты: номер часто лежит
		-- одним файлом, и на макетах бегунок простоял бы весь прогон в нуле.
		set doneCount to 0
		set totalCount to 0
		set nowName to ""
		try
			set stepText to do shell script ¬
				"/usr/bin/grep -oE '^\\[[0-9]+/[0-9]+\\] .*' " & ¬
				quoted form of outPath & " | /usr/bin/tail -n 1"
			set closeAt to offset of "]" in stepText
			if closeAt > 2 then
				set parts to my splitText(text 2 thru (closeAt - 1) of stepText, "/")
				set doneCount to (item 1 of parts) as integer
				set totalCount to (item 2 of parts) as integer
				set nowName to my trimTo(text (closeAt + 2) thru -1 of stepText, 30)
			end if
		end try

		-- Чем прогон занят прямо сейчас: export_pdf.py печатает такие строки
		-- ПЕРЕД началом каждого шага. Строка этапа всегда идёт после своей
		-- «[i/n]», поэтому последняя найденная относится к текущей полосе.
		set nowStage to ""
		try
			set stageText to do shell script "/usr/bin/grep -oE 'этап +: .*' " & ¬
				quoted form of outPath & " | /usr/bin/tail -n 1"
			set atColon to offset of ":" in stageText
			if atColon > 0 then
				set nowStage to my trimTo(text (atColon + 2) thru -1 of stageText, 46)
			end if
		end try

		try
			if totalCount > 0 then
				set progress total steps to totalCount
				set progress completed steps to (doneCount - 1)
			end if
			-- Подпись держим короткой намеренно: окно индикатора растёт
			-- по длине текста.
			set line2 to ""
			if totalCount > 0 then
				set line2 to doneCount & " из " & totalCount
				if nowName is not "" then set line2 to line2 & " · " & nowName
			else
				set line2 to "подготовка"
			end if
			if nowStage is not "" then set line2 to line2 & " · " & nowStage
			set progress additional description to line2 & " · " & (waited as integer) & " с"
		end try
	end repeat

	set outText to ""
	try
		set outText to do shell script "/bin/cat " & quoted form of outPath
	end try
	cleanUpRun(outPath, donePath, pidPath)
	return {rc, outText}
end runInBackground

-- Каталог наш, так что за собой убираем сами: иначе файлы прогонов копятся
-- в Caches без конца. Сбой уборки прогону не помеха — она добровольная.
on cleanUpRun(outPath, donePath, pidPath)
	try
		do shell script "/bin/rm -f " & quoted form of outPath & " " & ¬
			quoted form of donePath & " " & quoted form of pidPath
	end try
end cleanUpRun
