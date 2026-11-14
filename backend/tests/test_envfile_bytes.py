"""set_var must change one line and leave every other byte alone (CRLF, non-UTF-8)."""
import stat

from hunterai import config, envfile


def test_crlf_file_keeps_its_line_endings_on_update(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"# note\r\nA=1\r\n\r\nABLIT_API_KEY=old\r\nB=2\r\n")
    envfile.set_var(f, "ABLIT_API_KEY", "new")
    assert f.read_bytes() == b"# note\r\nA=1\r\n\r\nABLIT_API_KEY=new\r\nB=2\r\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_crlf_file_appends_with_crlf(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\r\nB=2\r\n")
    envfile.set_var(f, "OPENAI_API_KEY", "sk-1")
    assert f.read_bytes() == b"A=1\r\nB=2\r\nOPENAI_API_KEY=sk-1\r\n"


def test_crlf_file_without_trailing_newline_appends_with_crlf(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\r\nB=2")
    envfile.set_var(f, "OPENAI_API_KEY", "sk-1")
    assert f.read_bytes() == b"A=1\r\nB=2\r\nOPENAI_API_KEY=sk-1\r\n"


def test_update_of_last_line_keeps_missing_trailing_newline(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\r\nABLIT_API_KEY=old")
    envfile.set_var(f, "ABLIT_API_KEY", "new")
    assert f.read_bytes() == b"A=1\r\nABLIT_API_KEY=new"


def test_mixed_line_endings_are_each_preserved(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\nB=2\r\nABLIT_API_KEY=old\nC=3\r\n")
    envfile.set_var(f, "ABLIT_API_KEY", "new")
    assert f.read_bytes() == b"A=1\nB=2\r\nABLIT_API_KEY=new\nC=3\r\n"


def test_non_utf8_byte_does_not_stop_an_update(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"# caf\xe9\nA=\xff\xfe\nABLIT_API_KEY=old\nB=2\n")
    envfile.set_var(f, "ABLIT_API_KEY", "new")
    assert f.read_bytes() == b"# caf\xe9\nA=\xff\xfe\nABLIT_API_KEY=new\nB=2\n"


def test_non_utf8_byte_does_not_stop_an_append(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"A=\xff\r\n")
    envfile.set_var(f, "XAI_API_KEY", "k")
    assert f.read_bytes() == b"A=\xff\r\nXAI_API_KEY=k\r\n"


def test_non_ascii_value_is_written_as_utf8(tmp_path):
    f = tmp_path / ".env"
    f.write_bytes(b"ABLIT_MODEL=old\n")
    envfile.set_var(f, "ABLIT_MODEL", "mé")
    assert f.read_bytes() == "ABLIT_MODEL=mé\n".encode("utf-8")


def test_config_env_reads_valid_lines_despite_non_utf8_byte(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_bytes(b"# caf\xe9\nJUNK=\xff\xfe\nHUNTERAI_TEST_ONLY_VAR=good\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(f))
    monkeypatch.delenv("HUNTERAI_TEST_ONLY_VAR", raising=False)
    assert config.env("HUNTERAI_TEST_ONLY_VAR") == "good"
    assert config.env("HUNTERAI_TEST_ONLY_MISSING") is None


def test_config_env_reads_crlf_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\r\nHUNTERAI_TEST_ONLY_VAR=good\r\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(f))
    monkeypatch.delenv("HUNTERAI_TEST_ONLY_VAR", raising=False)
    assert config.env("HUNTERAI_TEST_ONLY_VAR") == "good"


def test_saved_key_is_the_one_read_back_after_a_form_feed(tmp_path, monkeypatch):
    # The writer splits on real newlines only; the reader must agree, or a saved key is ignored.
    f = tmp_path / ".env"
    f.write_bytes(b"A=1\x0cABLIT_API_KEY=old\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(f))
    monkeypatch.delenv("ABLIT_API_KEY", raising=False)
    envfile.set_var(f, "ABLIT_API_KEY", "new")
    assert config.env("ABLIT_API_KEY") == "new"
