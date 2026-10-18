import pytest

from meteoagent import config

BASE = """
[location]
latitude = 44.5
longitude = 11.3
elevation_m = 60

[telegram]
enabled = false
"""


@pytest.fixture
def load(tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "LLM_KEY"):
        monkeypatch.delenv(var, raising=False)

    def _load(extra: str, **env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        path = tmp_path / "config.toml"
        path.write_text(BASE + extra, encoding="utf-8")
        return config.load(path)

    return _load


class TestLLM:
    def test_anthropic_defaults(self, load):
        cfg = load("[llm]\n", ANTHROPIC_API_KEY="sk-ant-x")
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.model_name == "claude-haiku-4-5-20251001"
        assert cfg.llm.key_env == "ANTHROPIC_API_KEY"
        assert cfg.llm.api_key == "sk-ant-x"

    def test_legacy_claude_section_still_works(self, load):
        cfg = load('[claude]\nmodel = "claude-x"\n', ANTHROPIC_API_KEY="sk-ant-x")
        assert cfg.llm.model_name == "claude-x"
        assert cfg.llm.api_key == "sk-ant-x"

    def test_both_sections_rejected(self, load):
        with pytest.raises(config.ConfigError, match="not both"):
            load("[llm]\nenabled = false\n[claude]\nenabled = false\n")

    def test_api_key_env_is_configurable(self, load):
        cfg = load('[llm]\napi_key_env = "LLM_KEY"\n', LLM_KEY="k", ANTHROPIC_API_KEY="other")
        assert cfg.llm.api_key == "k"

    def test_openai_compatible_with_github_pat(self, load):
        cfg = load(
            "[llm]\n"
            'provider = "openai"\n'
            'endpoint = "https://models.github.ai/inference/chat/completions"\n'
            'model = "openai/gpt-4.1-mini"\n'
            'api_key_env = "GITHUB_TOKEN"\n',
            GITHUB_TOKEN="github_pat_x",
        )
        assert cfg.llm.provider == "openai"
        assert cfg.llm.api_key == "github_pat_x"

    @pytest.mark.parametrize(
        "body, match",
        [
            ('provider = "copilot"', "invalid \\[llm\\].provider"),
            ('provider = "openai"\nmodel = "m"\napi_key_env = "K"', "requires \\[llm\\].endpoint"),
            ('provider = "openai"\nendpoint = "https://x"\napi_key_env = "K"', "requires \\[llm\\].model"),
            ('provider = "openai"\nendpoint = "https://x"\nmodel = "m"', "requires \\[llm\\].api_key_env"),
            ('endpoint = "https://x"', "only used with provider = 'openai'"),
            ('api_key_env = "sk-ant-api03-abcdef"', "NAME of an environment variable"),
            ('api_key = "sk-ant-api03-abcdef"', "does not belong in the TOML"),
        ],
    )
    def test_invalid(self, load, body, match):
        with pytest.raises(config.ConfigError, match=match):
            load(f"[llm]\nenabled = false\n{body}\n")

    def test_missing_key_names_the_variable(self, load):
        with pytest.raises(config.ConfigError, match="GITHUB_TOKEN is not in the environment"):
            load(
                '[llm]\nprovider = "openai"\nendpoint = "https://x"\nmodel = "m"\n'
                'api_key_env = "GITHUB_TOKEN"\n'
            )

    def test_disabled_needs_no_key(self, load):
        assert not load("[llm]\nenabled = false\n").llm.enabled


class TestArpaeInterval:
    def test_default_is_every_cycle(self, load):
        assert load("[llm]\nenabled = false\n").arpae.interval_minutes == 0

    def test_multiple_of_cycle_interval(self, load):
        cfg = load("[llm]\nenabled = false\n[arpae]\ninterval_minutes = 30\n")
        assert cfg.arpae.interval_minutes == 30

    @pytest.mark.parametrize("value, match", [(20, "multiple"), (-15, ">= 0")])
    def test_invalid(self, load, value, match):
        with pytest.raises(config.ConfigError, match=match):
            load(f"[llm]\nenabled = false\n[arpae]\ninterval_minutes = {value}\n")
