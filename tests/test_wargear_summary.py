import pytest

pytest.importorskip("fastapi")

from app.main import _format_wargear_summary


def test_format_wargear_summary_labels_model_specific_copy_selections():
    options = [
        {"key": "bolt-pistol", "name": "Bolt pistol"},
        {"key": "plasma-pistol", "name": "Plasma pistol"},
    ]
    model_composition = [
        {
            "key": "aspiring-champion",
            "name": "Aspiring Champion",
            "wargear_options": [
                {"key": "bolt-pistol", "name": "Bolt pistol"},
                {"key": "plasma-pistol", "name": "Plasma pistol"},
            ],
        },
        {
            "key": "legionaries",
            "name": "Legionaries",
            "wargear_options": [
                {"key": "bolt-pistol", "name": "Bolt pistol"},
            ],
        },
    ]

    assert _format_wargear_summary(
        {
            "legionaries::bolt-pistol": 4,
            "aspiring-champion::plasma-pistol": 1,
        },
        options,
        model_composition,
    ) == "1x Aspiring Champion: Plasma pistol, 4x Legionaries: Bolt pistol"


def test_format_wargear_summary_keeps_legacy_flat_selections():
    assert _format_wargear_summary(
        {"bolt-pistol": 2},
        [{"key": "bolt-pistol", "name": "Bolt pistol"}],
    ) == "2x Bolt pistol"
