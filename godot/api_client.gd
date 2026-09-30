extends RefCounted
## Transport only. Validation, persistence and state transitions live in the service.

var base_url := "http://127.0.0.1:8765"

func request(host: Node, method: int, path: String, body: Dictionary = {}) -> Dictionary:
	var http := HTTPRequest.new()
	http.timeout = 8.0
	http.body_size_limit = 4 * 1024 * 1024
	host.add_child(http)
	var headers := PackedStringArray(["Accept: application/json", "Content-Type: application/json"])
	var payload := "" if body.is_empty() else JSON.stringify(body)
	var error := http.request(base_url + path, headers, method, payload)
	if error != OK:
		http.queue_free()
		return {"ok": false, "message": "서비스에 연결할 수 없습니다. Python 서비스를 먼저 실행해 주세요.", "code": "transport"}
	var result: Array = await http.request_completed
	http.queue_free()
	if result[0] != HTTPRequest.RESULT_SUCCESS:
		return {"ok": false, "message": "서비스 응답이 없습니다. 서비스 실행 상태와 주소를 확인해 주세요.", "code": "transport"}
	var parser := JSON.new()
	if parser.parse(result[3].get_string_from_utf8()) != OK or not parser.data is Dictionary:
		return {"ok": false, "message": "서비스가 올바른 JSON 응답을 반환하지 않았습니다.", "code": "invalid_response"}
	var data: Dictionary = parser.data
	if result[1] < 200 or result[1] >= 300:
		var service_error: Dictionary = data.get("error", {})
		return {"ok": false, "message": str(service_error.get("message", "요청을 처리하지 못했습니다.")), "code": str(service_error.get("code", "server")), "httpStatus": result[1]}
	data["ok"] = true
	return data
