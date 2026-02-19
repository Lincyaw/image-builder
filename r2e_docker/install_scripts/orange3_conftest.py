"""
Pytest conftest for Orange3 test runs inside the R2E Docker environment.

Ensures a headless Qt platform is set before any Orange import, and
suppresses common noisy warnings that would otherwise pollute test output.
"""

import os
import warnings

import pytest


# ---------------------------------------------------------------------------
# Headless Qt / display setup
# ---------------------------------------------------------------------------

# Force offscreen / minimal platform so PyQt5 works without a real display.
# xvfb-run is used in the outer test command, but setting these env vars as
# well prevents crashes when xvfb isn't available in edge cases.
os.environ.setdefault("QT_QPA_PLATFORM", "minimal")
os.environ.setdefault("DISPLAY", ":99")

# Orange tries to locate datasets via this path; point at the symlinked dir
# created during install (ln -s Orange/tests/datasets/ datasets).
os.environ.setdefault("ORANGE_DATA_DIR", "/testbed/datasets")


# ---------------------------------------------------------------------------
# Warning filters
# ---------------------------------------------------------------------------


def pytest_configure(config):
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
    warnings.filterwarnings("ignore", category=ResourceWarning)
    # numpy.distutils deprecation noise
    warnings.filterwarnings(
        "ignore",
        message=".*numpy.distutils.*",
        category=DeprecationWarning,
    )
    # PyQt5 / SIP deprecation noise
    warnings.filterwarnings(
        "ignore",
        message=".*sipPyTypeDict.*",
    )


# ---------------------------------------------------------------------------
# Session-scoped QApplication fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    """Create a single QApplication for the whole test session.

    Orange widgets require a QApplication to be alive; creating one here
    avoids ``RuntimeError: QApplication has not been created`` failures.
    """
    try:
        from AnyQt.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        yield app
    except Exception:
        # If Qt isn't available at all, just yield None so tests can handle it
        yield None
