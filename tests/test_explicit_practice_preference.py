"""Assert the actual writer contract, without treating fixture prose as quality evidence."""

from __future__ import annotations

import json
from contextlib import suppress

import pytest

from tests.test_recipe_whole_session import WholeSessionAdapter, run
from whoopy.meditation import GenerationError


@pytest.mark.parametrize(
    ("prompt", "anchor", "excluded"),
    [
        (
            "I am seated after an overstimulating workday. Guide a grounding practice "
            "using the contact of my feet with the floor and my body with the chair. "
            "Let ordinary sounds remain in the background. Develop one simple contact "
            "practice with time to try it and permission to adjust my position.",
            "contact",
            [],
        ),
        ("Let sounds stay in the background; use contact with the seat.", "contact", []),
        ("Use contact while ordinary sounds remain in the background.", "contact", []),
        ("Use contact and background sound.", "contact", []),
        ("Use sound for orientation and contact as the main practice.", "contact", []),
        ("Use contact for orientation and sound as the main practice.", "sound", []),
        ("Sounds come from outside. Focus on contact with the chair.", "contact", []),
        ("Contact is the main anchor. Ordinary sounds are present.", "contact", []),
        ("Use sound. Contact with the chair stays in the background.", "sound", []),
        ("Use natural breathing; sounds can remain in the background.", "natural_breath", []),
        ("Use contact for orientation. Focus on natural breathing.", "natural_breath", []),
        ("Use sound as the primary anchor; contact is also present.", "sound", []),
        ("Use contact and sounds.", "sound", []),
        ("Sound or contact would be fine.", "sound", []),
        ("A simple seated meditation.", "natural_breath", []),
        ('The phrase "use sound" is just an example. Use contact.', "contact", []),
        ('Someone said "avoid contact". Use contact with the chair.', "contact", []),
        ('Please use "sound" as my main anchor.', "sound", []),
        (
            "No contact; use natural breathing with sounds in the background.",
            "natural_breath",
            ["contact"],
        ),
        ("Use contact. Do not use sound or breathing.", "contact", ["natural_breath", "sound"]),
        ("Use sound as an alternative. Observe natural breathing.", "natural_breath", []),
        (
            "Observe natural breathing. If attending to breath is uncomfortable, "
            "offer contact with the chair instead. Do not use counting, breath holds, "
            "or controlled breathing.",
            "natural_breath",
            [],
        ),
        (
            "Use sound. If listening is overwhelming, offer contact instead.",
            "sound",
            [],
        ),
        ("Breathing is uncomfortable. Use contact.", "contact", ["natural_breath"]),
        (
            "Observe natural breathing. Do not deliberately control my breathing.",
            "natural_breath",
            [],
        ),
        (
            "Do not guide breath attention or prescribe a rhythm. Use contact.",
            "contact",
            ["natural_breath"],
        ),
    ],
)
def test_writer_receives_primary_practice_without_promoting_context(
    prompt: str, anchor: str, excluded: list[str]
) -> None:
    adapter = WholeSessionAdapter()
    # Some fixture scripts deliberately violate an exclusion; the contract is
    # available before validation and is the behavior under test here.
    with suppress(GenerationError):
        run(adapter, prompt, 180)
    assert adapter.requests
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    assert contract["original_user_need"] == prompt
    assert contract["anchor"] == anchor
    assert contract["excluded_anchors"] == excluded
    anchor_beat = next(
        beat for beat in contract["beats_in_order"] if beat["section_id"] == "anchor"
    )
    assert f"One {anchor} anchor" in anchor_beat["required_content"]
    if anchor != "natural_breath":
        assert "no breath attention" in contract["breath_focus"] or "natural_breath" in excluded


def test_excluding_every_anchor_still_stops_before_writer_invocation() -> None:
    adapter = WholeSessionAdapter()
    with pytest.raises(GenerationError, match="All supported anchors are excluded"):
        run(adapter, "No contact, no sound, and no breathing.", 180)
    assert adapter.requests == []
