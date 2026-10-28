from unittest.mock import Mock

from screenqa.capture import RegionWatcher


def _watcher():
    return RegionWatcher(
        {"left": 0, "top": 0, "width": 320, "height": 240},
        on_settled=Mock(),
        fast_probe=False,
    )


def test_paused_watcher_discards_queued_force_without_capturing():
    watcher = _watcher()
    watcher._grab_full = Mock()
    watcher._fire = Mock()
    watcher.paused = True
    watcher.force()

    watcher._tick(object())

    watcher._grab_full.assert_not_called()
    watcher._fire.assert_not_called()
    assert not watcher._force_evt.is_set()


def test_force_captures_once_after_resume():
    watcher = _watcher()
    shot = object()
    watcher._grab_full = Mock(return_value=shot)
    watcher._fire = Mock()
    watcher.paused = True
    watcher.force()
    watcher._tick(object())

    watcher.paused = False
    watcher.force()
    watcher._tick(object())

    watcher._grab_full.assert_called_once()
    watcher._fire.assert_called_once_with(shot)
