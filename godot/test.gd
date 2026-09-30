extends SceneTree
## Run against a disposable v0.1 service DB, never an operational user DB.
## godot --headless --path godot --script res://test.gd
## Then restart the service with the same DB and pass: -- --verify-id=<printed ID>

const SAVED_TITLE := "한글 업무 저장·수정 확인 — 날인본 검토"
var app

func _initialize() -> void:
	_run.call_deferred()

func _run() -> void:
	app = load("res://Main.tscn").instantiate()
	root.add_child(app)
	var deadline := Time.get_ticks_msec() + 15000
	while (not app.ready_for_writes or app.busy) and Time.get_ticks_msec() < deadline:
		await process_frame
	if not _check(app.ready_for_writes and not app.busy, "UI bootstrap/health failed"):
		return
	if not _check(app.statuses.has("in_progress"), "Service status contract failed"):
		return
	var verify_id := ""
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--verify-id="):
			verify_id = argument.trim_prefix("--verify-id=")
	if not verify_id.is_empty():
		app.selected_id = verify_id
		await app._load_selected()
		if not _check(app.selected_item.get("title") == SAVED_TITLE and app.selected_item.get("status") == "in_progress", "Restart persistence failed"):
			return
		print("GODOT_RESTART_PERSISTENCE_OK id=" + verify_id)
		await _finish()
		return
	var evidence_index: int = app.visible_ids.find("synthetic-task-001")
	if not _check(evidence_index >= 0, "Synthetic evidence fixture missing; start service with --demo"):
		return
	await app._select_work(evidence_index)
	if not _check("가상 펀드 A의 날인본을 요청했습니다." in app.evidence_view.text and "synthetic://demo/request-001" in app.evidence_view.text, "Evidence excerpt/locator display failed"):
		return
	app.create_title.text = "한글 업무 생성 확인"
	await app._create_work()
	var item_id: String = app.selected_id
	if not _check(not item_id.is_empty() and app.selected_item.get("title") == "한글 업무 생성 확인", "Korean create/select failed"):
		return
	if not _check(item_id in app.visible_ids, "Created item missing from UI list"):
		return
	app.title_edit.text = SAVED_TITLE
	for index in range(app.status_edit.item_count):
		if app.status_edit.get_item_metadata(index) == "in_progress":
			app.status_edit.select(index)
	app.reason_edit.text = "가상 업무로 한글 수정과 변경 이력 검증"
	await app._save_selected()
	if not _check(app.selected_item.get("title") == SAVED_TITLE and app.selected_item.get("status") == "in_progress", "Korean edit/status save failed"):
		return
	await app._refresh()
	if not _check(app.selected_item.get("title") == SAVED_TITLE and app.selected_item.get("version") == 2, "Refresh/version failed"):
		return
	var audit = JSON.parse_string(app.audit_view.text)
	if not _check(audit is Array and audit.size() >= 2, "Audit history missing"):
		return
	var rejected: Dictionary = await app.api.request(app, HTTPClient.METHOD_PATCH, "/api/work-items/" + item_id.uri_encode(), {"patch": {"status": "completed"}, "expectedVersion": 2, "actorId": "smoke-test", "reason": "완료 근거 없는 상태 전이 거절 검증"})
	if not _check(not rejected.get("ok", false) and rejected.get("httpStatus") == 400, "Server did not reject unsupported completion"):
		return
	await app._refresh()
	if not _check(app.selected_item.get("status") == "in_progress" and app.selected_item.get("version") == 2, "Rejected completion mutated state"):
		return
	print("GODOT_SMOKE_OK id=" + item_id)
	print("IME input, visual rendering, clipboard and platform exports require separate manual checks.")
	await _finish()

func _check(condition: bool, failure: String) -> bool:
	if not condition:
		push_error(failure)
		quit(1)
		return false
	return true

func _finish() -> void:
	app.queue_free()
	await process_frame
	quit(0)
