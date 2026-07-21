from __future__ import annotations

from pytest import CaptureFixture

from serenity.cli import main


def test_help_exits_successfully(capsys: CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "Local-first" in capsys.readouterr().out


def test_config_show_prints_resolved_settings(capsys: CaptureFixture[str]) -> None:
    assert main(["config", "show", "--tts-voice", "test_voice"]) == 0

    output = capsys.readouterr().out
    assert "voice: test_voice" in output
