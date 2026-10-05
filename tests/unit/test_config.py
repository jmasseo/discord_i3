from __future__ import annotations

from pathlib import Path

import pytest

from i3bridge.config import ConfigError, RouterConfig, load_config


def test_defaults_without_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.toml", environ={})
    assert cfg.mud.name == "Discord"
    assert cfg.routers[0] == RouterConfig("*dalet", "97.107.133.86", 8787)
    assert cfg.discord_token == ""


def test_file_and_env_override(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mud]\nname = "Discord"\n[[routers]]\nname = "*i4"\nhost = "1.2.3.4"\nport = 8080\n'
        "[discord]\nguild_id = 5\n[i3]\nreconnect_min_seconds = 1\n"
    )
    cfg = load_config(
        path,
        environ={"I3B_DISCORD__GUILD_ID": "42", "I3B_LOG__JSON": "true", "DISCORD_TOKEN": "tok"},
    )
    assert cfg.routers == [RouterConfig("*i4", "1.2.3.4", 8080)]
    assert cfg.discord.guild_id == 42
    assert cfg.log.json is True
    assert cfg.i3.reconnect_min_seconds == 1.0
    assert cfg.discord_token == "tok"
    assert cfg.format.transliterate == "text"


def test_transliterate_accepts_legacy_booleans(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[format]\ntransliterate = true\n")
    assert load_config(path, environ={}).format.transliterate == "all"
    path.write_text("[format]\ntransliterate = false\n")
    assert load_config(path, environ={}).format.transliterate == "none"
    assert (
        load_config(path, environ={"I3B_FORMAT__TRANSLITERATE": "none"}).format.transliterate
        == "none"
    )


@pytest.mark.parametrize(
    ("text", "env"),
    [
        ('[mud]\nname = ""\n', {}),
        ('[mud]\nname = "Dis cord"\n', {}),
        ('[[routers]]\nname = "dalet"\nhost = "h"\nport = 1\n', {}),
        ("[bogus]\nx = 1\n", {}),
        ("[mud]\nbogus = 1\n", {}),
        ('[format]\nmultiline = "maybe"\n', {}),
        ('[format]\ntransliterate = "sometimes"\n', {}),
        ("", {"I3B_DISCORD__GUILD_ID": "abc"}),
        ("", {"I3B_NOPE__X": "1"}),
        ("", {"I3B_LOG__JSON": "maybe"}),
    ],
)
def test_validation_errors(tmp_path: Path, text: str, env: dict[str, str]) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(path, environ=env)
