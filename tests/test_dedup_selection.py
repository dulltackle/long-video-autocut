from types import SimpleNamespace

from video_auto_editor.dedup import _find_duplicate_groups


def test_find_duplicate_groups_uses_similarity_threshold():
    items = [
        SimpleNamespace(text="今天讲视频剪辑"),
        SimpleNamespace(text="今天讲视频剪辑。"),
        SimpleNamespace(text="完全不同"),
    ]

    assert _find_duplicate_groups(
        items,
        lambda item: item.text,
        {"duplicate_threshold": 0.7},
    ) == [{0, 1}]
