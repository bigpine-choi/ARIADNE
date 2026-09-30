extends Control
## v0.1 UI: one service, one persisted work-item source, no local business-rule engine.

const API = preload("res://api_client.gd")
const MENUS := ["Command Center", "Calendar", "Funds", "Work Queue", "Evidence & Audit"]
const MUTED := Color("9faec3")

var api = API.new()
var items: Array = []
var statuses: Dictionary = {}
var visible_ids: Array[String] = []
var selected_id := ""
var selected_item: Dictionary = {}
var active_menu := "Work Queue"
var busy := false
var ready_for_writes := false
var message: Label
var connection: Label
var summary: Label
var section_title: Label
var search: LineEdit
var filter: OptionButton
var work_list: ItemList
var content: HSplitContainer
var placeholder: Label
var refresh_button: Button
var new_button: Button
var save_button: Button
var title_edit: LineEdit
var status_edit: OptionButton
var reason_edit: LineEdit
var metadata: Label
var conditions: TextEdit
var evidence_view: TextEdit
var audit_view: TextEdit
var create_dialog: ConfirmationDialog
var create_title: LineEdit
var create_error: Label
var nav_buttons: Dictionary = {}

func _ready() -> void:
	var configured := OS.get_environment("ARIADNE_API_BASE")
	if not configured.is_empty():
		api.base_url = configured.trim_suffix("/")
	_build_ui()
	await _bootstrap()

func _build_ui() -> void:
	var app_theme := Theme.new()
	var system_font := SystemFont.new()
	system_font.font_names = PackedStringArray(["Malgun Gothic", "Noto Sans CJK KR", "Noto Sans", "Arial"])
	app_theme.default_font = system_font
	app_theme.default_font_size = 16
	theme = app_theme
	var background := ColorRect.new()
	background.color = Color("0e1420")
	background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	background.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(background)
	var margin := MarginContainer.new()
	margin.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	for side in ["left", "top", "right", "bottom"]:
		margin.add_theme_constant_override("margin_" + side, 20)
	add_child(margin)
	var page := VBoxContainer.new()
	page.add_theme_constant_override("separation", 14)
	margin.add_child(page)
	var header := HBoxContainer.new()
	page.add_child(header)
	var brand := _label("ARIADNE", 26)
	brand.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(brand)
	header.add_child(_label("v0.1 · 가상 업무 시제품", 14, MUTED))
	connection = _label("서비스 연결 확인 중", 14, MUTED)
	header.add_child(connection)
	message = _label("", 14, MUTED)
	message.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	message.custom_minimum_size.y = 38
	page.add_child(message)
	var body := HBoxContainer.new()
	body.size_flags_vertical = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 20)
	page.add_child(body)
	var sidebar := VBoxContainer.new()
	sidebar.custom_minimum_size.x = 180
	sidebar.add_theme_constant_override("separation", 10)
	body.add_child(sidebar)
	for menu_name in MENUS:
		var button := Button.new()
		button.text = menu_name
		button.alignment = HORIZONTAL_ALIGNMENT_LEFT
		button.custom_minimum_size.y = 44
		button.pressed.connect(_switch_menu.bind(menu_name))
		nav_buttons[menu_name] = button
		sidebar.add_child(button)
	var sidebar_space := Control.new()
	sidebar_space.size_flags_vertical = Control.SIZE_EXPAND_FILL
	sidebar.add_child(sidebar_space)
	var sidebar_note := _label("자료는 로컬 서비스에 저장됩니다.\n메일·AI 연동은 후속 범위입니다.", 13, MUTED)
	sidebar_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	sidebar.add_child(sidebar_note)
	var workspace := VBoxContainer.new()
	workspace.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	workspace.add_theme_constant_override("separation", 10)
	body.add_child(workspace)
	section_title = _label("Work Queue · 업무 목록", 22)
	workspace.add_child(section_title)
	summary = _label("업무를 불러오고 있습니다.", 14, MUTED)
	workspace.add_child(summary)
	var toolbar := HBoxContainer.new()
	toolbar.add_theme_constant_override("separation", 8)
	workspace.add_child(toolbar)
	search = LineEdit.new()
	search.placeholder_text = "제목·펀드·담당자 검색"
	search.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	search.text_changed.connect(func(_value): _apply_filters())
	toolbar.add_child(search)
	filter = OptionButton.new()
	filter.custom_minimum_size.x = 160
	filter.add_item("전체 상태")
	filter.set_item_metadata(0, "")
	filter.item_selected.connect(func(_index): _apply_filters())
	toolbar.add_child(filter)
	refresh_button = Button.new()
	refresh_button.text = "새로고침"
	refresh_button.pressed.connect(_refresh)
	toolbar.add_child(refresh_button)
	new_button = Button.new()
	new_button.text = "+ 업무 등록"
	new_button.pressed.connect(_show_create)
	toolbar.add_child(new_button)
	placeholder = _label("", 18, MUTED)
	placeholder.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	placeholder.size_flags_vertical = Control.SIZE_EXPAND_FILL
	placeholder.hide()
	workspace.add_child(placeholder)
	content = HSplitContainer.new()
	content.size_flags_vertical = Control.SIZE_EXPAND_FILL
	content.split_offset = 370
	workspace.add_child(content)
	work_list = ItemList.new()
	work_list.custom_minimum_size.x = 270
	work_list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	work_list.size_flags_vertical = Control.SIZE_EXPAND_FILL
	work_list.fixed_icon_size = Vector2i(0, 0)
	work_list.item_selected.connect(_select_work)
	content.add_child(work_list)
	var details := VBoxContainer.new()
	details.custom_minimum_size.x = 440
	details.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	details.add_theme_constant_override("separation", 10)
	content.add_child(details)
	details.add_child(_label("업무 상세", 19))
	title_edit = LineEdit.new()
	title_edit.placeholder_text = "목록에서 업무를 선택해 주세요."
	title_edit.editable = false
	details.add_child(title_edit)
	metadata = _label("", 14, MUTED)
	metadata.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	details.add_child(metadata)
	var status_row := HBoxContainer.new()
	status_row.add_child(_label("업무 상태", 14))
	status_edit = OptionButton.new()
	status_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	status_edit.disabled = true
	status_row.add_child(status_edit)
	save_button = Button.new()
	save_button.text = "변경 저장"
	save_button.disabled = true
	save_button.pressed.connect(_save_selected)
	status_row.add_child(save_button)
	details.add_child(status_row)
	reason_edit = LineEdit.new()
	reason_edit.placeholder_text = "변경 사유 (필수)"
	reason_edit.text = "사용자 검토 후 업무 수정"
	details.add_child(reason_edit)
	var tabs := TabContainer.new()
	tabs.size_flags_vertical = Control.SIZE_EXPAND_FILL
	details.add_child(tabs)
	conditions = _read_only_text("기한 · 완료조건")
	tabs.add_child(conditions)
	evidence_view = _read_only_text("근거")
	tabs.add_child(evidence_view)
	audit_view = _read_only_text("변경 이력")
	tabs.add_child(audit_view)
	var copy := Button.new()
	copy.text = "선택 업무 요약 복사"
	copy.pressed.connect(_copy_selected)
	details.add_child(copy)
	_build_create_dialog()
	_switch_menu(active_menu)

func _build_create_dialog() -> void:
	create_dialog = ConfirmationDialog.new()
	create_dialog.title = "업무 등록"
	create_dialog.ok_button_text = "등록"
	create_dialog.cancel_button_text = "취소"
	create_dialog.dialog_hide_on_ok = false
	create_dialog.min_size = Vector2i(500, 230)
	create_dialog.confirmed.connect(_create_work)
	add_child(create_dialog)
	var fields := VBoxContainer.new()
	fields.add_theme_constant_override("separation", 14)
	create_dialog.add_child(fields)
	fields.add_child(_label("업무명", 16))
	create_title = LineEdit.new()
	create_title.placeholder_text = "예: 날인본 수령 및 검토"
	create_title.max_length = 300
	fields.add_child(create_title)
	fields.add_child(_label("기한·담당자·펀드는 미확정으로 등록합니다.\n완료 여부는 서비스가 완료조건과 근거를 검사합니다.", 14, MUTED))
	create_error = _label("", 14, Color("ffafaf"))
	create_error.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	fields.add_child(create_error)

func _label(value: String, font_size: int, color := Color("e7eef9")) -> Label:
	var label := Label.new()
	label.text = value
	label.add_theme_font_size_override("font_size", font_size)
	label.add_theme_color_override("font_color", color)
	return label

func _read_only_text(tab_name: String) -> TextEdit:
	var field := TextEdit.new()
	field.name = tab_name
	field.editable = false
	field.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	field.custom_minimum_size.y = 230
	field.size_flags_vertical = Control.SIZE_EXPAND_FILL
	return field

func _bootstrap() -> void:
	_set_busy(true)
	var health: Dictionary = await api.request(self, HTTPClient.METHOD_GET, "/api/health")
	if not health.get("ok", false):
		_show_error(health)
		_set_busy(false)
		return
	var status_response: Dictionary = await api.request(self, HTTPClient.METHOD_GET, "/api/statuses")
	if not status_response.get("ok", false):
		_show_error(status_response)
		_set_busy(false)
		return
	statuses.clear()
	for state in status_response.get("items", []):
		statuses[str(state.get("id", ""))] = str(state.get("label", ""))
	if statuses.is_empty():
		_show_error({"message": "서비스 상태 목록이 비어 있습니다."})
		_set_busy(false)
		return
	filter.clear()
	filter.add_item("전체 상태")
	filter.set_item_metadata(0, "")
	status_edit.clear()
	for key in statuses:
		filter.add_item(str(statuses[key]))
		filter.set_item_metadata(filter.item_count - 1, key)
		status_edit.add_item(str(statuses[key]))
		status_edit.set_item_metadata(status_edit.item_count - 1, key)
	ready_for_writes = true
	connection.text = "로컬 서비스 연결됨"
	await _reload_items()
	_set_busy(false)

func _refresh() -> void:
	if busy:
		return
	if not ready_for_writes:
		await _bootstrap()
		return
	_set_busy(true)
	await _reload_items()
	if not selected_id.is_empty():
		await _load_selected()
	_set_busy(false)

func _reload_items() -> void:
	var response: Dictionary = await api.request(self, HTTPClient.METHOD_GET, "/api/work-items")
	if not response.get("ok", false):
		_show_error(response)
		return
	items = response.get("items", [])
	_apply_filters()
	message.text = "서버 저장값을 조회했습니다. 기한 위험은 업무 상태와 별도로 표시합니다."
	connection.text = "로컬 서비스 연결됨"

func _apply_filters() -> void:
	work_list.clear()
	visible_ids.clear()
	var query := search.text.strip_edges().to_lower()
	var status_filter := str(filter.get_item_metadata(filter.selected)) if filter.selected >= 0 else ""
	for item in items:
		var searchable := "%s %s %s" % [item.get("title", ""), _nullable(item.get("fundId")), _nullable(item.get("ownerId"))]
		if not query.is_empty() and not query in searchable.to_lower():
			continue
		if not status_filter.is_empty() and item.get("status") != status_filter:
			continue
		var risk := _risk_label(str(item.get("dueRisk", "unknown")))
		var row := "%s\n%s · %s" % [item.get("title", ""), statuses.get(item.get("status"), item.get("status", "")), risk]
		work_list.add_item(row)
		work_list.set_item_tooltip(work_list.item_count - 1, "%s\nID: %s" % [searchable, item.get("id", "")])
		visible_ids.append(str(item.get("id", "")))
		if item.get("id") == selected_id:
			work_list.select(work_list.item_count - 1)
	summary.text = "전체 %d건 · 표시 %d건 · 서버 기준 기한 위험" % [items.size(), visible_ids.size()]
	if not selected_id.is_empty() and not selected_id in visible_ids:
		_clear_selection()

func _select_work(index: int) -> void:
	if busy or index < 0 or index >= visible_ids.size():
		return
	selected_id = visible_ids[index]
	_set_busy(true)
	await _load_selected()
	_set_busy(false)

func _load_selected() -> void:
	var requested_id := selected_id
	var response: Dictionary = await api.request(self, HTTPClient.METHOD_GET, "/api/work-items/" + requested_id.uri_encode())
	if requested_id != selected_id:
		return
	if not response.get("ok", false):
		_clear_selection()
		_show_error(response)
		return
	selected_item = response.get("item", {})
	title_edit.text = str(selected_item.get("title", ""))
	metadata.text = "펀드: %s · 담당: %s\n버전: %s · 기한 위험: %s · 신뢰도: %s" % [_nullable(selected_item.get("fundId")), _nullable(selected_item.get("ownerId")), selected_item.get("version", ""), _risk_label(str(selected_item.get("dueRisk", "unknown"))), selected_item.get("confidence", "")]
	for index in range(status_edit.item_count):
		if status_edit.get_item_metadata(index) == selected_item.get("status"):
			status_edit.select(index)
	conditions.text = _format_conditions(selected_item)
	var evidence_parts: Array[String] = []
	for evidence_id in selected_item.get("sources", []):
		var evidence_response: Dictionary = await api.request(self, HTTPClient.METHOD_GET, "/api/evidence/" + str(evidence_id).uri_encode())
		if evidence_response.get("ok", false):
			evidence_parts.append(_format_evidence(evidence_response.get("evidence", {})))
		else:
			evidence_parts.append("근거 %s 조회 실패: %s" % [evidence_id, evidence_response.get("message", "")])
	evidence_view.text = "\n\n────────────────────────\n\n".join(evidence_parts) if not evidence_parts.is_empty() else "연결된 근거가 없습니다. 완료·확정 판단에 사용할 근거를 먼저 등록해야 합니다."
	var audit_response: Dictionary = await api.request(self, HTTPClient.METHOD_GET, "/api/work-items/" + selected_id.uri_encode() + "/audit")
	audit_view.text = JSON.stringify(audit_response.get("entries", []), "  ") if audit_response.get("ok", false) else "이력 조회 실패: " + str(audit_response.get("message", ""))

func _format_conditions(item: Dictionary) -> String:
	var due: Dictionary = item.get("due", {})
	var lines: Array[String] = ["기한", "유형: %s · 확정: %s" % [due.get("kind", "unknown"), "예" if due.get("confirmed", false) else "아니오"], "날짜: %s · 시간대: %s" % [_nullable(due.get("date")), due.get("timezone", "Asia/Seoul")], "기한 원문: %s" % due.get("rawText", ""), "", "완료조건"]
	if due.get("kind") == "window":
		lines.insert(4, "기간: %s ~ %s" % [_nullable(due.get("startDate")), _nullable(due.get("endDate"))])
	elif due.get("kind") == "recurrence":
		lines.insert(4, "반복 원문 규칙: %s (일정 자동계산은 후속 범위)" % due.get("rule", ""))
	elif due.get("kind") == "event":
		lines.insert(4, "선행 업무 ID: %s" % _nullable(due.get("anchorTaskId")))
	for condition in item.get("completionConditions", []):
		lines.append("• %s (ID: %s)" % [condition.get("label", ""), condition.get("id", "")])
	if item.get("completionConditions", []).is_empty():
		lines.append("등록된 완료조건 없음")
	lines.append("\n완료 근거 및 검토")
	lines.append(JSON.stringify(item.get("completionEvidence", []), "  "))
	lines.append("\n사용자 확정 필드")
	lines.append(", ".join(item.get("confirmedFields", [])))
	return "\n".join(lines)

func _format_evidence(evidence: Dictionary) -> String:
	return "근거 ID: %s\n원천: %s / %s\n위치: %s\n확정성: %s · 역할: %s\n수집: %s\n페이지: %s · 조항: %s\n\n%s" % [evidence.get("id", ""), evidence.get("provider", ""), evidence.get("sourceId", ""), evidence.get("locator", ""), evidence.get("finality", ""), evidence.get("role", ""), evidence.get("capturedAt", ""), evidence.get("page", "—"), evidence.get("clause", "—"), evidence.get("evidence", "")]

func _save_selected() -> void:
	if busy or selected_item.is_empty() or status_edit.selected < 0:
		return
	if title_edit.text.strip_edges().is_empty() or reason_edit.text.strip_edges().is_empty():
		message.text = "업무명과 변경 사유를 입력해 주세요."
		return
	_set_busy(true)
	var patch := {"title": title_edit.text.strip_edges(), "status": str(status_edit.get_item_metadata(status_edit.selected))}
	var response: Dictionary = await api.request(self, HTTPClient.METHOD_PATCH, "/api/work-items/" + selected_id.uri_encode(), {"patch": patch, "expectedVersion": int(selected_item.get("version")), "actorId": "local-user", "reason": reason_edit.text.strip_edges()})
	if not response.get("ok", false):
		_show_error(response)
		if response.get("httpStatus") == 409:
			message.text += " 다른 변경이 반영됐습니다. 입력을 확인한 뒤 새로고침해 주세요."
	else:
		await _reload_items()
		if not selected_id.is_empty():
			await _load_selected()
		message.text = "변경과 감사 이력을 서버에 저장했습니다."
	_set_busy(false)

func _show_create() -> void:
	if busy or not ready_for_writes:
		return
	create_title.text = ""
	create_error.text = ""
	create_dialog.popup_centered()
	create_title.grab_focus()

func _create_work() -> void:
	if busy:
		return
	var title := create_title.text.strip_edges()
	if title.is_empty():
		create_error.text = "업무명을 입력해 주세요."
		return
	_set_busy(true)
	create_dialog.get_ok_button().disabled = true
	var item := {"id": "work-ui-%d-%d" % [int(Time.get_unix_time_from_system()), randi()], "title": title, "fundId": null, "ownerId": null, "status": "new", "reviewRequired": true, "confidence": "low", "due": {"kind": "unknown", "rawText": "기한 근거 미등록", "date": null, "timezone": "Asia/Seoul", "anchorTaskId": null, "confirmed": false, "evidenceIds": []}, "sources": [], "completionConditions": [], "completionEvidence": []}
	var response: Dictionary = await api.request(self, HTTPClient.METHOD_POST, "/api/work-items", {"item": item, "actorId": "local-user", "reason": "사용자가 업무명으로 신규 등록"})
	if not response.get("ok", false):
		create_error.text = str(response.get("message", "등록 실패"))
	else:
		search.text = ""
		filter.select(0)
		selected_id = str(response.get("item", {}).get("id", item["id"]))
		create_dialog.hide()
		await _reload_items()
		await _load_selected()
		message.text = "신규 업무를 저장했습니다. 기한·완료 여부는 미확정입니다."
	create_dialog.get_ok_button().disabled = false
	_set_busy(false)

func _switch_menu(menu_name: String) -> void:
	active_menu = menu_name
	for key in nav_buttons:
		nav_buttons[key].modulate = Color("91baff") if key == menu_name else Color.WHITE
	section_title.text = menu_name + " · v0.1"
	var later_scope := menu_name == "Calendar" or menu_name == "Funds"
	content.visible = not later_scope
	placeholder.visible = later_scope
	if later_scope:
		placeholder.text = ("계약문서 기반 자동 일정과 달력은 v0.4~v0.5에서 구현합니다." if menu_name == "Calendar" else "펀드별 계약·관계기관·정기 일정 화면은 v0.5에서 구현합니다.") + "\n현재 업무의 펀드 식별자는 Work Queue 상세에서 확인할 수 있습니다."
	elif menu_name == "Evidence & Audit":
		message.text = "업무를 선택한 뒤 근거·변경 이력 탭에서 서버 기록을 확인하세요."
	elif menu_name == "Command Center":
		message.text = "v0.1은 업무 목록과 서버의 기한 위험을 제공합니다. 우선순위 자동 산정은 후속 범위입니다."

func _set_busy(value: bool) -> void:
	busy = value
	refresh_button.disabled = value
	new_button.disabled = value or not ready_for_writes
	search.editable = not value
	filter.disabled = value
	work_list.mouse_filter = Control.MOUSE_FILTER_IGNORE if value else Control.MOUSE_FILTER_STOP
	work_list.focus_mode = Control.FOCUS_NONE if value else Control.FOCUS_ALL
	if value:
		work_list.release_focus()
	title_edit.editable = not value and not selected_item.is_empty()
	status_edit.disabled = value or selected_item.is_empty()
	save_button.disabled = value or selected_item.is_empty()

func _clear_selection() -> void:
	selected_id = ""
	selected_item = {}
	title_edit.text = ""
	metadata.text = "목록에서 업무를 선택해 주세요."
	conditions.text = ""
	evidence_view.text = ""
	audit_view.text = ""
	title_edit.editable = false
	status_edit.disabled = true
	save_button.disabled = true

func _show_error(response: Dictionary) -> void:
	message.text = "처리 실패: " + str(response.get("message", "알 수 없는 오류"))
	if response.get("code") == "transport":
		connection.text = "서비스 연결 끊김 · 목록은 마지막 조회값"

func _copy_selected() -> void:
	if selected_item.is_empty():
		return
	DisplayServer.clipboard_set("%s\n상태: %s\n%s\n\n%s" % [selected_item.get("title", ""), statuses.get(selected_item.get("status"), ""), metadata.text, conditions.text])
	message.text = "선택 업무 요약을 클립보드에 복사했습니다."

func _nullable(value) -> String:
	return "미확정" if value == null or str(value).is_empty() else str(value)

func _risk_label(value: String) -> String:
	return {"none": "기한 내", "due_today": "오늘 기한", "overdue": "기한 초과", "unknown": "기한 확인 필요"}.get(value, value)
