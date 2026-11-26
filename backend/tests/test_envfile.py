import stat

import pytest

from hunterai import envfile

ORIGINAL = ("# Another tool's provider config (gitignored). Rotate these keys if they leak.\n"
            "ANTHROPIC_API_KEY=a\nABLIT_API_KEY=old\nOTHERTOOL_LLM=x\n\n"
            "# vision path\nOTHERTOOL_LLM_MODEL=y\n")


@pytest.fixture
def env(tmp_path):
    f = tmp_path / ".env"
    f.write_text(ORIGINAL)
    f.chmod(0o600)
    return f


def test_set_var_preserves_other_lines(env):
    envfile.set_var(env, "ABLIT_API_KEY", "new")
    assert env.read_text() == ORIGINAL.replace("ABLIT_API_KEY=old", "ABLIT_API_KEY=new")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_set_var_appends_when_missing(env):
    envfile.set_var(env, "OPENAI_API_KEY", "sk-1")
    assert env.read_text() == ORIGINAL + "OPENAI_API_KEY=sk-1\n"


def test_append_adds_newline_when_file_lacks_one(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OTHERTOOL_LLM=x")
    envfile.set_var(f, "XAI_API_KEY", "k")
    assert f.read_text() == "OTHERTOOL_LLM=x\nXAI_API_KEY=k\n"


def test_creates_missing_file_private(tmp_path):
    f = tmp_path / "sub.env"
    envfile.set_var(f, "GEMINI_API_KEY", "g")
    assert f.read_text() == "GEMINI_API_KEY=g\n" and stat.S_IMODE(f.stat().st_mode) == 0o600


def test_loose_permissions_are_tightened(env):
    env.chmod(0o644)
    envfile.set_var(env, "ABLIT_API_KEY", "new")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_only_first_matching_line_and_similar_names_untouched(tmp_path):
    f = tmp_path / ".env"
    f.write_text("MY_ABLIT_API_KEY=keep\n#ABLIT_API_KEY=commented\nexport ABLIT_API_KEY=old\n"
                 "ABLIT_API_KEY_2=keep\n")
    envfile.set_var(f, "ABLIT_API_KEY", "new")
    assert f.read_text() == ("MY_ABLIT_API_KEY=keep\n#ABLIT_API_KEY=commented\n"
                             "export ABLIT_API_KEY=new\nABLIT_API_KEY_2=keep\n")


@pytest.mark.parametrize("name", ["OTHERTOOL_LLM", "OTHERSVC_API_KEY", "PATH", "", "A=B"])
def test_set_var_rejects_other_names(env, name):
    with pytest.raises(ValueError):
        envfile.set_var(env, name, "v")
    assert env.read_text() == ORIGINAL


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "OTHERTOOL_LLM=x\nINJECT=1", "x\x00y"])
def test_set_var_rejects_control_characters(env, value):
    with pytest.raises(ValueError):
        envfile.set_var(env, "ABLIT_API_KEY", value)
    assert env.read_text() == ORIGINAL


def test_value_read_back_through_config(env, monkeypatch):
    from hunterai import config
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    envfile.set_var(env, "ABLIT_API_KEY", "  ak_with spaces#and=signs  ")
    assert config.env("ABLIT_API_KEY") == "ak_with spaces#and=signs"


def test_no_temp_files_left_behind(env):
    envfile.set_var(env, "ABLIT_API_KEY", "new")
    # (the shared fixture also keeps its own files in this directory)
    assert [p.name for p in env.parent.iterdir() if p.name.startswith(".env")] == [".env"]


def test_allowed_names():
    assert envfile.ALLOWED == {"ABLIT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                               "XAI_API_KEY", "GEMINI_API_KEY", "ABLIT_MODEL", "DISCORD_WEBHOOK_URL",
                               "HUNTERAI_PROVIDER_PROXY", "HUNTERAI_OLLAMA_NUM_CTX", "HUNTERAI_OLLAMA_MAX_CTX",
                               "HUNTERAI_COLIBRI_URL", "COLIBRI_API_KEY", "HUNTERAI_LLAMACPP_URL", "LLAMACPP_API_KEY",
                               "HUNTERAI_EMBED_ON_CPU"}
