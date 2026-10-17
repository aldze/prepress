(*
	Prepress.app — приложение-дроплет для препресс-обработки макетов InDesign.

	Как пользоваться:
	  • перетащить .indd (или папку с макетами) на иконку в Dock;
	  • либо запустить двойным кликом — предложит выбрать файлы.

	Дальше спрашивает режим:
	  «Проверка»   → prepress.py --dry-run  (ничего не меняет, только отчёт)
	  «Обработать» → боевой прогон          (спросит подтверждение)

	Двойной клик по иконке (без перетаскивания) открывает меню, где кроме
	«Выбрать файлы» есть «Настройки» — панель основных параметров из
	config.json. Сам JSON панель не разбирает: строки для показа и запись
	значений делает prepress.py (--config-menu / --config-set), там же
	и вся проверка значений.

	Отчёт .md по каждому макету открывается сам — это делает уже prepress.py.

	Пересборка после правок:
	    droplet/build.sh

	------------------------------------------------------------------------
	Пять правил, которыми продиктовано устройство этого файла. Все пять —
	результат замеров, а не осторожности впрок.

	1. ВЫЙТИ ВПЕРЁД ДО ВСЕГО ОСТАЛЬНОГО.
	   Приложение, запущенное броском файла на иконку, на передний план не
	   выходит. Окно, показанное в таком состоянии, открывается позади чужих:
	   кликнуть по нему нельзя, иконка в Dock прыгает, а человек видит «мяч»
	   и ждёт, пока не переключится руками. Поэтому activate — самое первое
	   действие обоих входов, ещё до чтения файлов, и повторяется перед
	   каждым окном.
	   Замер: без activate окно не выходит вперёд и за 35 с; с ним — 2.8 с.

	2. НИКАКОГО SYSTEM EVENTS.
	   Любое обращение к нему — Apple Event, требующий отдельного разрешения
	   «Автоматизация». Всё, что здесь нужно (проверить файл, найти .indd),
	   делается своими силами и шеллом.

	3. У ОКОН НЕ ДОЛЖНО БЫТЬ ТАЙМАУТА.
	   Окно показывается Apple Event'ом, а на него по умолчанию отведено
	   120 секунд. Если человек думает дольше, окно молча умирает, а скрипт
	   падает с -1712. Поэтому каждое окно обёрнуто в with timeout.

	4. ДОЛГАЯ РАБОТА — ТОЛЬКО В ФОНЕ.
	   do shell script держит главный поток до конца команды. Пока он держит,
	   приложение не разбирает события, и macOS показывает «мяч» — на боевом
	   прогоне это была минута «зависшего» приложения на каждый макет.
	   Поэтому prepress.py запускается в фон, а ждём его циклом с delay:
	   delay, в отличие от do shell script, event loop крутит. См.
	   runInBackground.
	   Замер: прогон 53 с. До — 53 с мяча, после — живой индикатор в Dock.

	5. ОДИН ПРОГОН ЗА РАЗ.
	   Прямое следствие правила 4: раз ожидание крутит event loop, апплет
	   принимает новый бросок на иконку прямо посреди идущего прогона. Двух
	   параллельных прогонов по одному макету достаточно, чтобы получить
	   гонку на переподключении связей и сохранении. Оба входа проходят
	   через claimRun.
*)

property PYTHON_BIN : "/usr/bin/python3"
-- Путь подставляет build.sh при сборке — в исходнике его быть не должно,
-- иначе проект не перенести на другую машину.
property PREPRESS_PY : "@@PREPRESS_PY@@"

property RUN_TIMEOUT : 3600
property DIALOG_TIMEOUT : 86400

-- Как часто опрашивать фоновый прогон (правило 4 из шапки). Полсекунды —
-- компромисс: индикатор в Dock обновляется живо, а на опрос уходит один
-- короткий /bin/cat дважды в секунду.
property POLL_INTERVAL : 0.5

property MAX_LISTED : 12
property APP_TITLE : "Prepress"
property SETTINGS_TITLE : "Prepress — настройки"

-- Идёт ли прогон прямо сейчас. Правило 5 из шапки.
property BUSY : false

-- Журнал шагов: ~/Library/Logs/prepress_droplet.log
-- Стоит копейки, а когда приложение начинает вести себя странно, только он
-- и отвечает на вопрос «где именно ушло время». Выключить: false.
property DEBUG_LOG : true


-- ===========================================================================
--  Входы
-- ===========================================================================

-- Двойной клик по иконке открывает меню: намерение неочевидно, в отличие от
-- броска файлов на иконку. При броске меню не показывается.
on run
	bringForward()
	logStep("--- запуск двойным кликом ---")
	if not claimRun() then return
	try
		set choice to askUser("Перетащите .indd на иконку — или выберите действие.", ¬
			{"Отмена", "Настройки", "Выбрать файлы"}, 3, "note")
		if choice is "Настройки" then
			showSettings()
		else if choice is "Выбрать файлы" then
			if prepressExists() then
				set chosen to pickFiles()
				if chosen is not {} then handleFiles(collectIndd(chosen))
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
		if prepressExists() then handleFiles(collectIndd(droppedItems))
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
-- просто закрывал окно, и снаружи это выглядело как «панель не работает» —
-- без единой подсказки о причине. Отмену (-128) не показываем: это не сбой.
on reportFailure(errMsg, errNum)
	if errNum is -128 then return
	logStep("  СБОЙ (" & errNum & "): " & errMsg)
	try
		tellUser("Что-то пошло не так:" & return & return & errMsg & return & ¬
			return & "Код ошибки: " & errNum & return & return & ¬
			"Подробности — в ~/Library/Logs/prepress_droplet.log", "stop")
	end try
end reportFailure


-- Правило 5 из шапки. Оба входа проходят только отсюда, поэтому второй прогон
-- физически не может стартовать поверх первого.
on claimRun()
	if BUSY then
		logStep("  отказ: прогон уже идёт")
		tellUser("Прогон уже идёт." & return & return & ¬
			"Дождитесь его окончания — отчёт откроется сам.", "caution")
		return false
	end if
	set BUSY to true
	return true
end claimRun


-- ===========================================================================
--  Передний план
-- ===========================================================================

-- Правило 1 из шапки. activate вызывается дважды с паузой: одиночный вызов
-- в момент запуска может не успеть отработать — приложение ещё поднимается,
-- и запрос на выход вперёд теряется.
on bringForward()
	try
		activate
		delay 0.2
		activate
	end try
end bringForward


-- ===========================================================================
--  Окна
-- ===========================================================================
--
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
			"Logs/prepress_droplet.log"
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
--  Панель настроек
-- ===========================================================================
--
-- Панель ничего не знает про формат config.json: prepress.py --config-menu
-- отдаёт готовые строки «ключ / раздел / подпись / значение / тип / варианты»,
-- разделённые табуляцией, а --config-set принимает одно «ключ=значение» и сам
-- его проверяет. Поэтому здесь нет ни разбора JSON, ни знания о допустимых
-- значениях — только показ и ввод. Новая настройка в prepress.py появляется
-- в панели сама, править этот файл не нужно.
--
-- Правило 4 сюда не относится: вызовы --config-menu и --config-set занимают
-- доли секунды, в отличие от прогона макета, — фон тут был бы лишней сложностью.

on runPy(argsText)
	return do shell script quoted form of PYTHON_BIN & " " & ¬
		quoted form of PREPRESS_PY & " " & argsText
end runPy

-- Окно индикатора и окна панели подгоняются под длину текста, поэтому всё,
-- что приходит извне (имена файлов, строки лога), обязано быть обрезано.
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

-- «значение=подпись;значение=подпись» -> {{значение, подпись}, ...}
on parseOptions(optText)
	set out to {}
	if optText is "" then return out
	repeat with chunk in my splitText(optText, ";")
		set pair to my splitText(chunk as text, "=")
		if (count of pair) is 2 then
			set end of out to {item 1 of pair, item 2 of pair}
		end if
	end repeat
	return out
end parseOptions

on labelsOf(pairs)
	set out to {}
	repeat with pr in pairs
		set end of out to item 2 of pr
	end repeat
	return out
end labelsOf

on valueForLabel(pairs, lbl)
	repeat with pr in pairs
		if (item 2 of pr) is lbl then return item 1 of pr
	end repeat
	return ""
end valueForLabel

-- Список с выбором. Правило 3 распространяется и на него: choose from list —
-- такой же Apple Event, и без снятого таймаута умирает через 120 секунд.
-- Возвращает {} и на отмену, и на «ОК» без выделения: для вызывающего это одно
-- и то же — выбора нет.
--
-- default items обязателен. Без него список открывается вообще без выделения,
-- «ОК» отдаёт пустой список, обращение к item 1 роняет обработчик — и панель
-- просто закрывалась. Предвыбранная первая строка убирает и саму возможность
-- такого нажатия, и лишний клик.
on pickFromList(theItems, thePrompt, okName, cancelName)
	if (count of theItems) is 0 then return {}
	bringForward()
	with timeout of DIALOG_TIMEOUT seconds
		set picked to choose from list theItems with title SETTINGS_TITLE ¬
			with prompt thePrompt default items {item 1 of theItems} ¬
			OK button name okName cancel button name cancelName
	end timeout
	if picked is false then return {}
	return picked
end pickFromList

-- Окно редактирования одного поля. Возвращает "" — значит, отменили.
on askValue(fieldLabel, fieldKind, shownValue, optText)
	if fieldKind is "bool" then
		-- Переключатель: спрашивать «да или нет» отдельным окном незачем.
		if shownValue is "да" then
			return "нет"
		else
			return "да"
		end if

	else if fieldKind is "choice" or fieldKind is "mode" then
		set pairs to my parseOptions(optText)
		-- Здесь «Отмена» честная: значение ещё не записано, отказ ничего
		-- не меняет. В отличие от списка параметров, см. showSettings.
		set picked to my pickFromList(my labelsOf(pairs), ¬
			fieldLabel & return & "Сейчас: " & shownValue, "Выбрать", "Отмена")
		if picked is {} then return ""
		return my valueForLabel(pairs, item 1 of picked)

	else if fieldKind is "file" then
		bringForward()
		try
			with timeout of DIALOG_TIMEOUT seconds
				set f to choose file with prompt ¬
					fieldLabel & " (сейчас: " & shownValue & ")"
			end timeout
		on error number -128
			return ""
		end try
		return POSIX path of f

	else
		set hint to ""
		if fieldKind is "number" then
			set limits to my splitText(optText, ";")
			if (count of limits) is 2 then
				set hint to return & "Допустимо: от " & (item 1 of limits) & ¬
					" до " & (item 2 of limits)
			end if
		end if
		bringForward()
		try
			with timeout of DIALOG_TIMEOUT seconds
				set dlg to display dialog fieldLabel & hint ¬
					default answer shownValue buttons {"Отмена", "OK"} ¬
					default button 2 cancel button 1 with title SETTINGS_TITLE
			end timeout
		on error number -128
			return ""
		end try
		return text returned of dlg
	end if
end askValue

on showSettings()
	logStep("панель настроек")
	-- Каждое значение пишется в config.json сразу же, поэтому кнопка выхода
	-- ничего не отменяет. Пока не правили ничего — «Отмена» ещё честна; после
	-- первой же записи она стала бы враньём, и там уже «Готово».
	set touched to false
	repeat
		try
			set menuText to runPy("--config-menu")
		on error errMsg
			tellUser("Не удалось прочитать настройки:" & return & return & errMsg, "stop")
			return
		end try

		-- Разбираем строки и заодно собираем подписи для списка.
		set rows to {}
		set labels to {}
		repeat with ln in paragraphs of menuText
			set f to my splitText(ln as text, tab)
			if (count of f) is 6 then
				set end of rows to f
				set end of labels to (item 2 of f) & " · " & (item 3 of f) & ":  " & (item 4 of f)
			end if
		end repeat

		if (count of rows) is 0 then
			tellUser("prepress.py не вернул ни одной настройки.", "stop")
			return
		end if

		if touched then
			set exitName to "Готово"
		else
			set exitName to "Отмена"
		end if
		set picked to my pickFromList(labels, ¬
			"Выберите параметр, чтобы изменить его." & return & ¬
			"Остальное правится в config.json.", "Изменить", exitName)
		if picked is {} then
			logStep("  панель закрыта")
			return
		end if

		-- Ищем выбранную строку: подписи в списке уникальны.
		set idx to 0
		repeat with i from 1 to count of labels
			if (item i of labels) is (item 1 of picked) then set idx to i
		end repeat
		if idx is 0 then return

		set f to item idx of rows
		set theKey to item 1 of f
		set fieldLabel to item 3 of f
		set shownValue to item 4 of f
		set fieldKind to item 5 of f
		set optText to item 6 of f

		set newValue to my askValue(fieldLabel, fieldKind, shownValue, optText)
		if newValue is not "" then
			try
				runPy("--config-set " & quoted form of (theKey & "=" & newValue))
				set touched to true
				logStep("  " & theKey & " = " & newValue)
				-- Тихо: новое значение и так видно в списке следующим витком.
			on error errMsg
				logStep("  ОТКАЗ " & theKey & ": " & errMsg)
				tellUser("Не сохранено." & return & return & errMsg, "caution")
			end try
		end if
	end repeat
end showSettings


-- ===========================================================================
--  Файлы
-- ===========================================================================

-- Правило 2: без System Events. Коэрция в alias — своя, ничего не требует.
on prepressExists()
	try
		POSIX file PREPRESS_PY as alias
		return true
	end try
	tellUser("Не найден prepress.py:" & return & return & PREPRESS_PY & return & return & ¬
		"Проект переехал? Пересоберите приложение: droplet/build.sh", "stop")
	return false
end prepressExists

on baseName(posixPath)
	return item -1 of my splitText(posixPath, "/")
end baseName

-- Из брошенного набора собираем список .indd. Папки просматриваем рекурсивно.
on collectIndd(theItems)
	set found to {}
	repeat with itm in theItems
		set p to POSIX path of itm
		if p ends with ".indd" then
			set end of found to p
		else
			try
				set res to do shell script "/usr/bin/find " & quoted form of p & ¬
					" -type f -iname '*.indd' -not -name '.*' | /usr/bin/sort"
				repeat with ln in paragraphs of res
					set s to ln as text
					if s is not "" then set end of found to s
				end repeat
			end try
		end if
	end repeat
	logStep("найдено макетов: " & (count of found))
	return found
end collectIndd


-- ===========================================================================
--  Основной сценарий
-- ===========================================================================

on handleFiles(inddPaths)
	set n to count of inddPaths
	if n = 0 then
		tellUser("Не нашёл ни одного файла .indd." & return & return & ¬
			"Перетащите на иконку файл вёрстки или папку с макетами.", "caution")
		return
	end if

	-- Показываем, что именно будет обработано: в папках COL1-2 рядом лежат
	-- две версии вёрстки (имя.indd и имя-i26.indd), их легко перепутать.
	set listText to ""
	set shown to 0
	repeat with p in inddPaths
		if shown < MAX_LISTED then
			set listText to listText & "• " & baseName(p) & return
			set shown to shown + 1
		end if
	end repeat
	if n > shown then set listText to listText & "… и ещё " & (n - shown) & return

	try
		set choice to askUser("Файлов вёрстки: " & n & return & return & listText, ¬
			{"Отмена", "Проверка", "Обработать"}, 2, "note")
	on error number -128
		return
	end try
	if choice is "Отмена" then return

	set flagPart to ""
	if choice is "Проверка" then
		set flagPart to " --dry-run"
	else
		-- Боевой прогон необратим на месте, а иконка в Dock — лёгкая мишень.
		try
			set confirmed to askUser("Боевой прогон изменит файлы:" & return & return & ¬
				"• картинки станут CMYK TIFF" & return & ¬
				"• связи переподключатся" & return & ¬
				"• результат сохранится в новый файл с суффиксом _CMYK" & return & return & ¬
				"Макетов: " & n & ". Продолжить?", ¬
				{"Отмена", "Обработать"}, 1, "caution")
		on error number -128
			return
		end try
		if confirmed is not "Обработать" then return
	end if

	runAll(inddPaths, flagPart)
end handleFiles

on runAll(inddPaths, flagPart)
	set n to count of inddPaths
	set okCount to 0
	set failCount to 0
	set failLog to ""

	repeat with i from 1 to n
		set p to item i of inddPaths as text
		set nm to baseName(p)
		logStep("прогон " & i & "/" & n & ": " & nm)

		-- Индикатор в Dock; на некоторых системах недоступен — не критично.
		-- Стартовое состояние. Пока prepress.py не напечатал первую строку
		-- «[i/n]», числа картинок мы не знаем — показываем неопределённость
		-- честно, а не «файл 1 из 1» при стоящем бегунке.
		try
			set progress total steps to 0
			set progress completed steps to 0
			set progress description to "Prepress: " & trimTo(nm, 50)
			if n > 1 then
				set progress additional description to "Макет " & i & " из " & n & " · запуск"
			else
				set progress additional description to "Запуск"
			end if
		end try

		-- -u обязателен. Вывод уходит в файл, а не в терминал, поэтому Python
		-- буферизует его блоками по несколько килобайт: без -u индикатор
		-- показывал бы ход работы пачками с большим запозданием.
		set cmd to quoted form of PYTHON_BIN & " -u " & quoted form of PREPRESS_PY & ¬
			flagPart & " " & quoted form of p

		set res to runInBackground(cmd, i, n, nm)
		set rc to item 1 of res
		set outText to item 2 of res

		if rc = 0 then
			set okCount to okCount + 1
			logStep("  готово")
		else
			set failCount to failCount + 1
			set failLog to failLog & "• " & nm & return & "   " & outText & return
			logStep("  ОШИБКА (код " & rc & "): " & outText)
		end if
	end repeat

	-- Шагами индикатора к этому моменту были картинки последнего макета,
	-- а не макеты, — поэтому доводим до конца обе величины разом.
	try
		set progress total steps to n
		set progress completed steps to n
	end try

	-- Окно об успехе не показываем: об окончании и так сообщает открывшийся
	-- .md-отчёт. А вот про сбой сказать надо — по упавшему файлу отчёта не будет.
	if failCount > 0 then
		tellUser("Успешно: " & okCount & " из " & n & return & ¬
			"С ошибкой: " & failCount & return & return & failLog, "stop")
	end if
end runAll


-- Правило 4 из шапки. Возвращает {код возврата, вывод прогона}.
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
on runInBackground(cmd, i, n, nm)
	-- Рабочие файлы прогона держим В СВОЁМ каталоге, а не в общем
	-- TemporaryItems. Тот защищён: создать файл в нём можно, а увидеть или
	-- удалить чужой — «Operation not permitted». Достаточно, чтобы кто-то
	-- однажды положил туда файл с тем же именем (хватит и запуска этого же
	-- скрипта не из приложения), и приложение перестаёт работать насовсем:
	-- оно падает на попытке убрать «свой» файл ещё до старта прогона.
	-- Имена вдобавок уникальны, поэтому чистить перед прогоном нечего.
	set runDir to (POSIX path of (path to library folder from user domain)) & ¬
		"Caches/local.prepress.droplet/"
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
			return {-1, "Прогон не уложился в " & RUN_TIMEOUT & " секунд и был остановлен."}
		end if

		-- Ход работы берём не из последней строки вывода подряд, а только из
		-- строк вида «[3/11] имя.jpg» — их prepress.py печатает на каждую
		-- картинку. Причины две.
		--
		-- Во-первых, по ним двигается сам бегунок: шаги индикатора — это
		-- картинки текущего макета, а не макеты. Раньше шагами были макеты,
		-- и на одиночном файле (обычный случай) бегунок стоял в нуле весь
		-- прогон, сообщая только «файл 1 из 1».
		--
		-- Во-вторых, длина. Произвольная строка лога бывает в сотню символов
		-- (предупреждение про ИИ-апскейл), а окно индикатора подгоняется под
		-- текст — и уезжало за край экрана. Имя картинки поэтому обрезаем.
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

		-- Чем прогон занят прямо сейчас. prepress.py печатает такие строки
		-- ПЕРЕД началом каждого долгого этапа (см. log_stage) — до этого
		-- ИИ-апскейл молчал по несколько минут, и понять, что происходит,
		-- было нельзя. Строка этапа всегда идёт после своей «[i/n]», поэтому
		-- последняя найденная относится к текущей картинке.
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
			-- Подпись держим короткой намеренно: окно индикатора растёт по
			-- длине текста. Отсюда и «3 из 9» вместо «картинка 3 из 9»,
			-- и жёсткая обрезка имени.
			set line2 to ""
			if n > 1 then set line2 to "макет " & i & "/" & n & " · "
			if totalCount > 0 then
				set line2 to line2 & doneCount & " из " & totalCount
				if nowName is not "" then set line2 to line2 & " · " & nowName
			else
				set line2 to line2 & "подготовка"
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
