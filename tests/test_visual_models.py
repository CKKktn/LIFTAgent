"""Visual model configuration, actual image connection probes, and legacy choices."""
import base64
from io import BytesIO
import json
from unittest.mock import patch

from PIL import Image
import pytest

from backend.drawing_evidence import DRAWING_RECOGNITION_METHOD
import fake_model_provider
from fake_model_provider import FakeProvider
from test_backend import data, first_dataset, parse, upload, workspace


def save_visual_model(client, base_url="https://synthetic.invalid/v1"):
    response = client.post("/settings/models", json={
        "name": "synthetic-visual-model", "model_type": "vision", "base_url": base_url,
        "api_key": "synthetic-test-key", "set_default": True,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_visual_model_has_its_own_configuration_and_default(workspace):
    app, client = workspace
    chat = client.post("/settings/models", json={
        "name": "synthetic-chat-model", "model_type": "chat", "base_url": "https://synthetic.invalid/v1",
        "set_default": True,
    }).json()
    visual = save_visual_model(client)
    assert app.state.models.default_id("chat") == chat["id"]
    assert app.state.models.default_id("vision") == visual["id"]
    settings = client.get("/settings/models").json()
    assert settings["vision_configured"]
    assert next(row for row in settings["models"] if row["id"] == visual["id"])["model_type"] == "vision"
    assert "synthetic-test-key" not in json.dumps(settings)
    configured = data(client.patch("/api/v1/models/default", json={"model_type": "vision", "model_id": visual["id"]}))
    assert any(row["model_type"] == "vision" and row["model_id"] == visual["id"] and row["configured"]
               for row in configured)
    edited = client.post("/settings/models", json={**visual, "api_key": ""})
    assert edited.status_code == 200 and edited.json()["api_key_set"]
    assert app.state.models.vision_config(visual["id"], private=True)["api_key"] == "synthetic-test-key"

    dataset = first_dataset(client)
    configured = data(client.put(f"/api/v1/datasets/{dataset}", json={
        "parser_config": {"drawing_vision_model": visual["id"]},
    }))
    assert configured["parser_config"]["drawing_vision_model"] == visual["id"]
    assert client.delete("/settings/models/" + visual["id"]).status_code == 409


@pytest.mark.parametrize(("probe_code", "model_answer", "http_status"), [
    (731842, "731842", 200), (381005, "图片里的数字是 381005。", 200), (731842, "OK", 502),
])
def test_visual_connection_test_sends_an_image_and_checks_its_answer(workspace, monkeypatch, probe_code, model_answer, http_status):
    _, client = workspace
    captured = []

    def answer(messages):
        captured.append(messages[-1]["content"])
        return model_answer

    monkeypatch.setattr(fake_model_provider, "answer_for", answer)
    with FakeProvider() as provider:
        visual = save_visual_model(client, provider.base_url)
        with patch("backend.models.randbelow", return_value=probe_code):
            response = client.post("/settings/models/" + visual["id"] + "/test")
    assert response.status_code == http_status, response.text
    assert len(captured) == 1 and isinstance(captured[0], list)
    [image_part] = [block for block in captured[0] if block["type"] == "image_url"]
    uri = image_part["image_url"]["url"]
    assert uri.startswith("data:image/png;base64,")
    with Image.open(BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as image:
        assert image.width >= 300 and image.height >= 100
        darkest, lightest = image.convert("L").getextrema()
        assert lightest - darkest > 200
    prompt = "".join(block["text"] for block in captured[0] if block["type"] == "text")
    assert str(probe_code) not in prompt
    if http_status == 200:
        assert response.json()["success"]
    else:
        assert "图片" in response.json()["message"]
    assert "synthetic-test-key" not in response.text


def test_legacy_drawing_chat_selection_still_parses(workspace, monkeypatch):
    app, client = workspace
    captured = []
    if DRAWING_RECOGNITION_METHOD == "ocr_vision":
        from backend.drawing_ocr import OCRReading
        monkeypatch.setattr("backend.drawing_evidence.recognize_drawing", lambda *_: OCRReading("", [], []))

    def answer(messages):
        captured.append(messages[-1]["content"])
        return json.dumps({
            "page_kind": "drawing", "page_text": "", "annotations": [{
                "object": "H1 H2", "annotation": "50 mm", "region_id": "page",
                "annotation_box": [100, 100, 400, 200], "object_box": [100, 300, 800, 800],
            }],
        })

    monkeypatch.setattr(fake_model_provider, "answer_for", answer)
    with FakeProvider() as provider:
        legacy = client.post("/settings/models", json={
            "name": "legacy-drawing-model", "model_type": "chat", "base_url": provider.base_url,
        }).json()
        dataset = first_dataset(client)
        data(client.put(f"/api/v1/datasets/{dataset}", json={"parser_config": {"drawing_vision_model": legacy["id"]}}))
        stream = BytesIO()
        image = Image.new("RGB", (400, 200), "white")
        image.putpixel((100, 100), (0, 0, 0))
        image.save(stream, format="PNG")
        document = upload(client, dataset, "legacy.png", stream.getvalue())
        parse(client, dataset, [document])
        assert len(captured) == 1 and isinstance(captured[0], list)
        assert app.state.models.vision_config(legacy["id"])["model_type"] == "chat"


def test_visual_connection_supports_image_apis_without_streaming(workspace, monkeypatch):
    _, client = workspace
    captured = []

    def respond(handler):
        body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
        captured.append(body)
        streaming = bool(body.get("stream"))
        payload = {"error": {"message": "Image API requires non-streaming requests"}} if streaming else {
            "id": "synthetic", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "731842"}, "finish_reason": "stop"}],
        }
        encoded = json.dumps(payload).encode()
        handler.send_response(400 if streaming else 200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)

    monkeypatch.setattr(fake_model_provider.ProviderHandler, "do_POST", respond)
    with FakeProvider() as provider:
        visual = save_visual_model(client, provider.base_url)
        with patch("backend.models.randbelow", return_value=731842):
            response = client.post("/settings/models/" + visual["id"] + "/test")
    assert captured, response.text
    assert response.status_code == 200, response.text
    assert len(captured) == 1 and captured[0]["stream"] is False


def test_changing_visual_endpoint_marks_only_its_knowledge_bases_for_reparse(workspace):
    app, client = workspace
    model = save_visual_model(client)
    dataset = first_dataset(client)
    data(client.put(f"/api/v1/datasets/{dataset}", json={"parser_config": {"drawing_vision_model": model["id"]}}))
    another = data(client.post("/api/v1/datasets", json={"name": "文本知识库"}))["id"]
    first = upload(client, dataset, "first.txt", "合成测试文字".encode())
    second = upload(client, another, "second.txt", "合成测试文字".encode())
    parse(client, dataset, [first])
    parse(client, another, [second])
    edited = client.post("/settings/models", json={**model, "name": "synthetic-visual-v2"})
    assert edited.status_code == 200 and edited.json()["requires_reparse"]
    assert app.state.knowledge.document(first["id"])["run"] == "UNSTART"
    assert app.state.knowledge.document(second["id"])["run"] == "DONE"
    data(client.put(f"/api/v1/datasets/{dataset}", json={"parser_config": {"drawing_vision_model": ""}}))
    assert client.delete("/settings/models/" + model["id"]).status_code == 200
    assert not any(row["model_id"] == model["id"] for row in client.get("/settings/models").json()["defaults"])
