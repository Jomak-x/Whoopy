import pytest

from scripts.chatterbox_v3_raw_meditation import parse_authored_script


@pytest.fixture
def authored_script() -> str:
    durations = [20] * 17 + [70, 60]
    return "\n\n".join(
        f"Rest in passage {index + 1}.\n\nKeep these exact words.\n\n"
        f"[{'Final silence' if index == 18 else 'Pause'} for {seconds} seconds]"
        for index, seconds in enumerate(durations)
    )


def test_authored_script_has_nineteen_speech_events(authored_script: str) -> None:
    events = parse_authored_script(authored_script)

    assert len(events) == 19
    assert sum(silence for _, silence in events) == 470
    assert events[0][0] == "Rest in passage 1.\n\nKeep these exact words."
    assert events[-1][0] == "Rest in passage 19.\n\nKeep these exact words."
    assert events[-1][1] == 60


def test_parser_preserves_words_but_removes_timing_markup(authored_script: str) -> None:
    events = parse_authored_script(authored_script)
    spoken = " ".join(text for text, _ in events)

    assert "[Pause" not in spoken
    assert "[Final silence" not in spoken
    assert spoken.count("Keep these exact words.") == 19
