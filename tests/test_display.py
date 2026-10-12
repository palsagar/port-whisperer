from __future__ import annotations

from portwhisperer.display import format_status


def test_detached_is_neutral_not_a_warning():
    # Detached only means "no parent shell", so it must not look like a warning
    assert format_status("detached") == "[blue]●[/] [blue]detached[/]"
