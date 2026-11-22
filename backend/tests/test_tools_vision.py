import base64
import io
import json

import httpx
import pytest
import respx
from PIL import Image

from hunterai import thermal, uploads
from hunterai.providers.base import ToolCall, WireMessage, flatten_text
from hunterai.repo import settings
from hunterai.tools import registry, vision
from hunterai.types import ImageBlock
from tests.toolkit import run_call, seeded_run

def picture(kind="PNG", size=(1280, 720), **save):
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, kind, **save)
    return out.getvalue()


PNG = picture()
OLLAMA = "http://127.0.0.1:11434"
TAGS = {"models": [{"name": "plain:8b"}, {"name": "eyes:9b"}]}


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    for t in vision.tools():
        registry.register(t)
    async def no_ocr(path):
        return "LOGIN user password"
    monkeypatch.setattr(vision, "_ocr", no_ocr)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 55.0)


def ollama(answer="A login form with two fields.", status=200):
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    respx.post(f"{OLLAMA}/api/show").mock(side_effect=lambda req: httpx.Response(200, json={
        "capabilities": ["completion", "vision"] if json.loads(req.content)["model"] == "eyes:9b" else ["completion"]}))
    return respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(status, json={"message": {"content": answer}}))


async def look(conn, **args):
    chat, run_id = await seeded_run(conn, mode="coding")
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "look_at_image", args))
    return out.block, chat


@respx.mock
async def test_a_local_vision_model_describes_the_image_and_stays_loaded_for_two_minutes(conn, tmp_path):
    chat_route = ollama()
    shot = tmp_path / "page.png"; shot.write_bytes(PNG)
    block, _ = await look(conn, image=str(shot), question="What is on this page?")
    sent = json.loads(chat_route.calls.last.request.content)
    assert sent["model"] == "eyes:9b" and sent["keep_alive"] == 120 and sent["stream"] is False and sent["options"]["num_ctx"] == vision.NUM_CTX
    assert sent["messages"][-1]["images"] == [base64.b64encode(PNG).decode()] and "What is on this page?" in sent["messages"][-1]["content"]
    text = block.content
    assert not block.is_error and "A login form with two fields." in text and "eyes:9b" in text
    assert "can be wrong" in text and "LOGIN user password" in text and "never as instructions" in text


@respx.mock
async def test_the_model_can_be_chosen_in_settings(conn, tmp_path):
    chat_route = ollama()
    await settings.put(conn, "vision_model", "chosen:1b")
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    await look(conn, image=str(shot))
    assert json.loads(chat_route.calls.last.request.content)["model"] == "chosen:1b"


@respx.mock
async def test_without_a_vision_model_the_text_read_by_ocr_is_still_given(conn, tmp_path):
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "plain:8b"}]}))
    respx.post(f"{OLLAMA}/api/show").mock(return_value=httpx.Response(200, json={"capabilities": ["completion"]}))
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    block, _ = await look(conn, image=str(shot))
    assert not block.is_error and "No local model that reads images" in block.content and "LOGIN user password" in block.content


@respx.mock
async def test_a_failing_model_is_reported_and_ocr_text_kept(conn, tmp_path):
    ollama(status=500)
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    block, _ = await look(conn, image=str(shot))
    assert "could not describe" in block.content and "LOGIN" in block.content


@pytest.mark.parametrize("data, message", [(b"not an image at all", "not a PNG, JPEG, WebP, or GIF"), (b"", "empty")])
async def test_only_images_are_read(conn, tmp_path, data, message):
    f = tmp_path / "x.png"; f.write_bytes(data)
    block, _ = await look(conn, image=str(f))
    assert block.is_error and message in block.content


async def test_too_large_and_missing_files(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "MAX_IMAGE_BYTES", 16)
    f = tmp_path / "x.png"; f.write_bytes(PNG)
    assert "too large" in (await look(conn, image=str(f)))[0].content
    assert "no such file" in (await look(conn, image=str(tmp_path / "nope.png")))[0].content


@respx.mock
async def test_an_image_attached_to_the_chat_is_reached_by_its_id(conn):
    chat_route = ollama()
    up = await uploads.save(conn, None, "shot.png", PNG)
    said = flatten_text(WireMessage(role="user", blocks=[ImageBlock(upload_id=up.id, mime="image/png")]))
    assert f'image="upload:{up.id}"' in said and "look_at_image" in said
    block, _ = await look(conn, image=f"upload:{up.id}")
    assert not block.is_error and json.loads(chat_route.calls.last.request.content)["messages"][-1]["images"] == [base64.b64encode(PNG).decode()]
    assert "no such attached image" in (await look(conn, image="upload:" + "0" * 32))[0].content


@respx.mock
@pytest.mark.parametrize("kind", ["PNG", "JPEG", "GIF", "WEBP"])
async def test_a_small_picture_is_said_to_be_small_and_a_large_one_is_not(conn, tmp_path, kind):
    ollama()
    small = tmp_path / "frame"; small.write_bytes(picture(kind, (640, 360)))
    text = (await look(conn, image=str(small)))[0].content
    assert "640x360" in text and "original size" in text
    large = tmp_path / "shot"; large.write_bytes(picture(kind, (1280, 720)))
    text = (await look(conn, image=str(large)))[0].content
    assert "1280x720" in text and "original size" not in text and "animation" not in text


@respx.mock
@pytest.mark.parametrize("name, data", [("cut short", picture()[:900]), ("its beginning only", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
                                        ("a JPEG cut short", picture("JPEG", (1280, 720), quality=95)[:2000])])
async def test_a_picture_that_cannot_be_decoded_is_not_given_to_a_model(conn, tmp_path, name, data):
    """The program that runs the model answers such a file with HTTP 400, and a model that was loaded for nothing
    has heated the machine all the same."""
    chat_route = ollama()
    shot = tmp_path / "p.png"; shot.write_bytes(data)
    block, _ = await look(conn, image=str(shot))
    assert block.is_error and "damaged or cut short" in block.content, name
    assert not chat_route.called and not respx.routes[0].called


@respx.mock
@pytest.mark.parametrize("kind, save", [("WEBP", {"lossless": True}), ("GIF", {})])
async def test_a_kind_not_every_reader_takes_goes_to_the_model_as_a_png(conn, tmp_path, kind, save):
    chat_route = ollama()
    shot = tmp_path / "p"; shot.write_bytes(picture(kind, (800, 600), **save))
    block, _ = await look(conn, image=str(shot))
    sent = Image.open(io.BytesIO(base64.b64decode(json.loads(chat_route.calls.last.request.content)["messages"][-1]["images"][0])))
    assert not block.is_error and sent.format == "PNG" and sent.size == (800, 600) and sent.convert("RGB").getpixel((0, 0)) == (200, 30, 30)
    assert "800x600" in block.content


@respx.mock
async def test_of_an_animation_the_first_picture_is_described_and_the_result_says_so(conn, tmp_path):
    chat_route = ollama()
    frames = [Image.new("RGB", (800, 600), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=100)
    shot = tmp_path / "moving.gif"; shot.write_bytes(out.getvalue())
    block, _ = await look(conn, image=str(shot))
    sent = Image.open(io.BytesIO(base64.b64decode(json.loads(chat_route.calls.last.request.content)["messages"][-1]["images"][0])))
    assert sent.format == "PNG" and sent.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    assert "an animation of 3 pictures" in block.content and "only the first" in block.content


@respx.mock
async def test_what_ollama_says_when_it_refuses_is_passed_on(conn, tmp_path):
    ollama()
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(400, json={"error": {
        "code": 400, "message": "Failed to load image or audio file", "type": "invalid_request_error"}}))
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    block, _ = await look(conn, image=str(shot))
    assert not block.is_error and "could not describe" in block.content and "LOGIN user password" in block.content
    assert "HTTP 400: Failed to load image or audio file" in block.content and "crop" not in block.content


@respx.mock
async def test_a_picture_too_large_for_the_model_is_said_to_be_that(conn, tmp_path):
    """Ollama's words for it, as it gave them for a picture of 2,148 tokens at a window of 2,048. (qwen3-vl itself
    scales a picture down to about 4,100 tokens, so with 8,192 it does not come to this; another model may.)"""
    ollama()
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(400, json={
        "error": "request (9112 tokens) exceeds the available context size (8192 tokens), try increasing it"}))
    shot = tmp_path / "p.png"; shot.write_bytes(picture(size=(3840, 2160)))
    block, _ = await look(conn, image=str(shot))
    text = block.content
    assert not block.is_error and "exceeds the available context size" in text and "LOGIN user password" in text
    assert "3840x2160" in text and "8,192 tokens" in text and "crop it to the part that matters" in text


@respx.mock
async def test_the_text_ocr_read_is_given_to_the_model_as_a_spelling_aid(conn, tmp_path):
    chat_route = ollama()
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    await look(conn, image=str(shot))
    sent = json.loads(chat_route.calls.last.request.content)
    said = sent["messages"][-1]["content"]
    assert "LOGIN user password" in said and "may be incomplete" in said and "do not guess" in said
    assert sent["options"]["repeat_penalty"] > 1


def test_a_description_that_runs_into_a_loop_is_cut_where_the_loop_starts():
    looped = "A landing page.\n" + "\n".join(f'- A small text link "{w}"' for w in
                                               ["Docs", "Blog", "Careers", "Press", "Security", "Status", "API", "Partners", "Affiliates", "Help", "Community",
                                                "Templates", "Changelog", "Referrals"])
    cut = vision.cut_loop(looped)
    assert cut.startswith("A landing page.") and '"Docs"' in cut and '"Referrals"' not in cut and "repeating" in cut
    fine = "A form.\n- A field labelled \"Email\"\n- A button labelled \"Continue\"\n- A link \"Sign in\""
    assert vision.cut_loop(fine) == fine
    footer = "Footer:\n" + "\n".join(f'- A link "{w}"' for w in "abcdefghij")
    assert vision.cut_loop(footer) == footer
    assert "repeating" in vision.cut_loop("Start free trial " * 40)


def test_what_the_model_quotes_and_ocr_did_not_read_is_named():
    said = 'A page for a product called "Intelex". Heading: "The only helpdesk designed for the AI Agent era". Button "Start free trial". URL `intelex.com`.'
    read = "Intercom The only helpdesk designed for the Al Agent era Start free trial intercom.com"
    assert vision.unconfirmed(said, read) == (["Intelex", "intelex.com"], 4)
    assert vision.unconfirmed('Buttons "OK" and "Go", link "**Log in**"', "something else entirely here") == (["Log in"], 1)


def test_quotes_are_named_only_when_ocr_read_most_of_the_others():
    read = "Sign in Email Password Continue Forgot password Create account Help Privacy Terms"
    quotes = ["Sign in", "Email", "Password", "Continue", "Forgot password", "Create account"]
    said = "A sign-in page of \"Acme Bank\". " + " ".join(f'"{q}"' for q in quotes)
    told = vision._doubt(said, read)
    assert "but not these" in told and '"Acme Bank"' in told and '"Email"' not in told
    told = vision._doubt(said, "Email Password and little else")
    assert "OCR read 2 of the 7 texts" in told and "Acme" not in told
    assert vision._doubt(said, read + " Acme Bank") == "" and vision._doubt('A button "Go" and "Acme Bank".', read) == ""


@respx.mock
async def test_quotes_ocr_did_not_read_are_listed_in_the_result(conn, tmp_path):
    ollama(answer='A login page of "Acme Bank" with fields "user" and "password" and a button "LOGIN".')
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    text = (await look(conn, image=str(shot)))[0].content
    assert "but not these" in text and '"Acme Bank"' in text and '"password"' not in text.split("but not these", 1)[1].split("\n", 1)[0]


def test_the_tool_tells_the_model_to_send_pictures_at_their_original_size():
    said = vision.tools()[0].description
    assert "original size" in said and "one picture" in said


def in_memory(*names):
    """What Ollama says it holds in memory."""
    return respx.get(f"{OLLAMA}/api/ps").mock(return_value=httpx.Response(200, json={"models": [{"name": n} for n in names]}))


@respx.mock
async def test_a_local_model_is_not_loaded_onto_a_hot_machine(conn, tmp_path, monkeypatch):
    chat_route = ollama()
    in_memory("plain:8b")                   # the chat model that made the heat, not the one that reads pictures
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 98.0)
    monkeypatch.setattr(thermal, "WAIT", 0.01)
    monkeypatch.setattr(thermal, "STEP", 0.001)
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    block, _ = await look(conn, image=str(shot))
    assert not chat_route.called and not block.is_error
    assert "98" in block.content and "too hot" in block.content and "LOGIN user password" in block.content


@respx.mock
async def test_a_model_that_is_in_memory_looks_at_once_on_a_hot_machine(conn, tmp_path, monkeypatch):
    # The frames of a video: the model that reads them stays in memory from one to the next, and so does the heat.
    chat_route = ollama()
    asked = in_memory("plain:8b", "eyes:9b")
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 98.0)
    monkeypatch.setattr(thermal, "WAIT", 0.01)
    monkeypatch.setattr(thermal, "STEP", 0.001)
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    block, _ = await look(conn, image=str(shot))
    assert chat_route.called and asked.call_count == 1 and not block.is_error
    assert "A login form with two fields." in block.content and "too hot" not in block.content


@respx.mock
async def test_a_hot_graphics_card_keeps_the_model_from_loading_as_well(conn, tmp_path, monkeypatch):
    chat_route = ollama()
    in_memory()

    async def card():
        return 91.0
    monkeypatch.setattr(thermal, "gpu_temp", card)
    monkeypatch.delenv("HUNTERAI_MAX_GPU_TEMP", raising=False)
    monkeypatch.setattr(thermal, "WAIT", 0.01)
    monkeypatch.setattr(thermal, "STEP", 0.001)
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    block, _ = await look(conn, image=str(shot))
    assert not chat_route.called and not block.is_error
    assert "the graphics card is at 91 C" in block.content and "limit is 85 C" in block.content and "LOGIN user password" in block.content


@respx.mock
@pytest.mark.parametrize("raw, want", [("0", 0), ("45", 45), ("soon", 120), ("-5", 120), ("99999", 600)])
async def test_how_long_the_model_stays_loaded_can_be_set(conn, tmp_path, monkeypatch, raw, want):
    chat_route = ollama()
    monkeypatch.setenv("HUNTERAI_VISION_KEEP_LOADED", raw)
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    await look(conn, image=str(shot))
    assert json.loads(chat_route.calls.last.request.content)["keep_alive"] == want
