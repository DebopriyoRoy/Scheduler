"""The app comes back on its stable port after a restart.

8765 is the address the user has bookmarked. A just-closed listener leaves the port in
TIME_WAIT for around a minute, and the first version gave up on the first refusal and
took a random high port instead - so an ordinary restart silently moved the app to
127.0.0.1:50288 and the bookmark stopped working. It happened three times in one day.
"""

import importlib.util
import socket
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "app_main", Path(__file__).resolve().parents[1] / "desktop" / "app_main.py"
)
app_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_main)


def _an_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_preferred_port_is_taken_when_it_is_free():
    port = _an_unused_port()
    assert app_main.free_port(preferred=port, wait_seconds=0) == port


def test_a_port_left_in_time_wait_is_still_claimed():
    """This is the restart case, and the one that kept breaking the bookmark."""
    port = _an_unused_port()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    listener.close()  # leaves the port in TIME_WAIT

    assert app_main.free_port(preferred=port, wait_seconds=0) == port


def test_a_port_someone_is_listening_on_is_given_up_on():
    """Another copy really is serving there; take any port rather than refusing to start."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    busy = listener.getsockname()[1]
    try:
        chosen = app_main.free_port(preferred=busy, wait_seconds=0)
        assert chosen != busy
        assert chosen > 0
    finally:
        listener.close()


@pytest.mark.parametrize("wait", [0, 0.2])
def test_it_always_returns_a_usable_port(wait):
    port = app_main.free_port(preferred=_an_unused_port(), wait_seconds=wait)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
