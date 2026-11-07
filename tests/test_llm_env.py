"""AnthropicClient reads ANTHROPIC_API_KEY / CLAUDE_MODEL from the environment, then from .env."""
import anthropic
import pytest

from pipeline import llm


@pytest.fixture
def fake(monkeypatch):
    calls = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    return calls


def test_key_and_model_from_dotenv(fake, tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nexport ANTHROPIC_API_KEY='sk-from-file'\nCLAUDE_MODEL=\"m-file\"\n")
    monkeypatch.setattr(llm, "_DOTENV", f)
    c = llm.AnthropicClient()
    assert fake == [{"api_key": "sk-from-file"}]
    assert c.model == "m-file"


def test_environment_beats_dotenv(fake, tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("ANTHROPIC_API_KEY=sk-file\nCLAUDE_MODEL=m-file\n")
    monkeypatch.setattr(llm, "_DOTENV", f)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    monkeypatch.setenv("CLAUDE_MODEL", "m-env")
    c = llm.AnthropicClient()
    assert fake == [{"api_key": "sk-env"}] and c.model == "m-env"


def test_no_key_raises_clear_error(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_DOTENV", tmp_path / "missing.env")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        llm.AnthropicClient()


def test_parser_ignores_junk_and_does_not_touch_environ(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("novalue\n  A = 1 \nB=x=y\n")
    monkeypatch.delenv("A", raising=False)
    assert llm._read_dotenv(f) == {"A": "1", "B": "x=y"}
    import os
    assert "A" not in os.environ
    assert llm._read_dotenv(tmp_path / "nope") == {}
