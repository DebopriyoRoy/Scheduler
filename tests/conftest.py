"""Test-wide isolation from the real application data.

The Square session lives in ~/Library/Application Support, outside the repository and
outside any database rollback. A test that exercises the sign-in paths will therefore
write into the running application's own session unless the directory is redirected -
which is exactly what happened: a test raising "the stored session has expired"
reached the view's expiry handler and stamped the real marker file, signing the live
application out.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_square_session(tmp_path, monkeypatch):
    """Point every test at a throwaway session directory."""
    monkeypatch.setenv("SPIRIT_SQUARE_SESSION_DIR", str(tmp_path / "square-session"))
