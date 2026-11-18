"""Driving the sidebar topic menu (a popover of buttons) in AppTest."""

NEW = "＋ 새 주제 만들기"
ADMIN = "⚙ 관리 (유지 보수)"
_SPECIAL = {"pick__new": NEW, "pick__admin": ADMIN}


def menu(at) -> list[str]:
    """Menu entries in order: topic IDs, then the special pages by label."""
    keys = [b.key for b in at.sidebar.button if b.key and b.key.startswith("pick_")]
    return [_SPECIAL.get(k, k[len("pick_"):]) for k in keys]


def go(at, entry: str):
    """Open a topic (by ID) or a special page (by label)."""
    key = {v: k for k, v in _SPECIAL.items()}.get(entry, f"pick_{entry}")
    at.button(key=key).click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at
