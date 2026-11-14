import base64
import json

import httpx
import pytest
import respx

from hunterai import thermal, uploads
from hunterai.providers.base import ToolCall, WireMessage, flatten_text
from hunterai.repo import settings
from hunterai.tools import registry, vision
from hunterai.types import ImageBlock
from tests.toolkit import run_call, seeded_run

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
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


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 32


def jpeg(width, height):
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def test_the_size_of_a_picture_is_read_from_its_header():
    assert vision.picture_size(png(640, 360)) == (640, 360)
    assert vision.picture_size(jpeg(1280, 720)) == (1280, 720)
    assert vision.picture_size(b"GIF89a" + (320).to_bytes(2, "little") + (200).to_bytes(2, "little") + b"\x00" * 8) == (320, 200)
    assert vision.picture_size(b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x0a\x00\x00\x00" + b"\x00" * 4
                               + (799).to_bytes(3, "little") + (599).to_bytes(3, "little")) == (800, 600)
    assert vision.picture_size(PNG) is None and vision.picture_size(b"\xff\xd8\xff" + b"\x00" * 9) is None


@respx.mock
async def test_a_small_picture_is_said_to_be_small_and_a_large_one_is_not(conn, tmp_path):
    ollama()
    small = tmp_path / "frame.png"; small.write_bytes(png(640, 360))
    text = (await look(conn, image=str(small)))[0].content
    assert "640x360" in text and "original size" in text
    large = tmp_path / "shot.png"; large.write_bytes(png(1280, 720))
    text = (await look(conn, image=str(large)))[0].content
    assert "1280x720" in text and "original size" not in text


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


@respx.mock
async def test_a_local_model_is_not_loaded_onto_a_hot_machine(conn, tmp_path, monkeypatch):
    chat_route = ollama()
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 98.0)
    monkeypatch.setattr(thermal, "WAIT", 0.01)
    monkeypatch.setattr(thermal, "STEP", 0.001)
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    block, _ = await look(conn, image=str(shot))
    assert not chat_route.called and not block.is_error
    assert "98" in block.content and "too hot" in block.content and "LOGIN user password" in block.content


@respx.mock
@pytest.mark.parametrize("raw, want", [("0", 0), ("45", 45), ("soon", 120), ("-5", 120), ("99999", 600)])
async def test_how_long_the_model_stays_loaded_can_be_set(conn, tmp_path, monkeypatch, raw, want):
    chat_route = ollama()
    monkeypatch.setenv("HUNTERAI_VISION_KEEP_LOADED", raw)
    shot = tmp_path / "p.png"; shot.write_bytes(PNG)
    await look(conn, image=str(shot))
    assert json.loads(chat_route.calls.last.request.content)["keep_alive"] == want
