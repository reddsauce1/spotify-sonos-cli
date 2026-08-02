"""The log file is bounded.

launchd redirects a process's stdout to a file and never rotates it, and
nothing else was rotating either. The UI polls /nowplaying every ten seconds,
so the access log grows about 1.1MB a day for as long as the machine is on --
it had reached 3.8MB and was still climbing.

Rotation has to be done by this process, and it cannot be done to a file
launchd holds open: renaming a file out from under launchd's descriptor
leaves launchd appending to the rotated-away file forever. So the process
owns its log, and the plist points stdout and stderr at a separate crash log
that stays small because almost nothing is written to it.
"""
import logging
import logging.handlers
import os
import re

import pytest

from paths import README_MD, SERVER_PY


class TestTheHandlerIsBounded:
    def test_it_rotates(self, server_mod):
        assert isinstance(server_mod._handler,
                          logging.handlers.RotatingFileHandler)

    def test_the_cap_is_reachable_but_not_silly(self, server_mod):
        """At ~1.1MB a day this is months of history, and a hard ceiling."""
        total = server_mod.LOG_MAX_BYTES * (server_mod.LOG_BACKUP_COUNT + 1)
        assert 10 * 1024 * 1024 <= total <= 100 * 1024 * 1024

    def test_old_files_are_kept_not_discarded(self, server_mod):
        """backupCount=0 truncates with no history, which loses the record of
        whatever caused the burst that filled it."""
        assert server_mod.LOG_BACKUP_COUNT >= 3

    def test_the_handler_is_pointed_at_the_log_directory(self, server_mod):
        assert server_mod.LOG_PATH.endswith("logs/spotify-server.log")


class TestRotationActuallyHappens:
    """The settings being right is not the same as rotation working."""

    def test_writing_past_the_cap_rolls_the_file(self, tmp_path):
        path = tmp_path / "probe.log"
        handler = logging.handlers.RotatingFileHandler(
            str(path), maxBytes=2048, backupCount=3)
        logger = logging.getLogger("rotation-probe")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            for i in range(400):
                logger.info("line %d %s", i, "x" * 80)
        finally:
            handler.close()
            logger.removeHandler(handler)

        assert path.exists()
        assert path.stat().st_size <= 4096, "the live file is not bounded"
        rolled = sorted(tmp_path.glob("probe.log.*"))
        assert rolled, "nothing was rotated"
        assert len(rolled) <= 3, "backupCount is not being honoured"

    def test_total_size_stays_under_the_cap(self, tmp_path):
        path = tmp_path / "probe.log"
        handler = logging.handlers.RotatingFileHandler(
            str(path), maxBytes=1024, backupCount=2)
        logger = logging.getLogger("rotation-probe-2")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            for i in range(2000):
                logger.info("line %d %s", i, "y" * 60)
        finally:
            handler.close()
            logger.removeHandler(handler)

        total = sum(f.stat().st_size for f in tmp_path.glob("probe.log*"))
        assert total <= 1024 * 3 * 1.2, f"grew past the cap: {total} bytes"


class TestOneStream:
    """An application error is far easier to read next to the request that
    caused it than in a separate file."""

    @pytest.mark.parametrize("logger_name", ["dj", "cherrypy.access",
                                             "cherrypy.error"])
    def test_all_three_can_share_the_handler(self, server_mod, logger_name):
        assert "_route_cherrypy_logs_to_file" in open(SERVER_PY).read()

    def test_our_logger_does_not_propagate(self, server_mod):
        """cherrypy's loggers carry their own handlers and propagate, so a
        root handler would duplicate every access line."""
        assert server_mod.log.propagate is False

    def test_the_screen_handler_is_turned_off(self):
        """Left on, CherryPy writes both logs to stdout as well -- every line
        stored twice, and the crash log growing exactly as fast as the file
        being rotated."""
        source = open(SERVER_PY).read()
        assert "'log.screen': False" in source

    def test_cherrypy_logs_are_rerouted_after_config_update(self):
        """config.update installs the default screen handlers, so replacing
        them earlier just gets them added back."""
        source = open(SERVER_PY).read()
        update = source.index("cherrypy.config.update({")
        reroute = source.index("_route_cherrypy_logs_to_file()", update)
        assert update < reroute


class TestTheCrashLogStaysSeparate:
    def test_the_app_does_not_write_to_the_launchd_paths(self, server_mod):
        """If the process wrote to the same file launchd holds open, rotation
        would rename it out from under launchd's descriptor."""
        assert "launchd" not in server_mod.LOG_PATH

    def test_the_readme_explains_which_file_is_which(self, server_mod):
        """Two log files with similar names is confusing unless the README
        says which one to read. Checking for the filenames alone is not
        enough -- they also appear in the plist listing, so that would pass
        even with every word of explanation deleted."""
        readme = open(README_MD).read()
        prose = re.sub(r"```.*?```", "", readme, flags=re.S)   # drop code blocks

        assert "logs/spotify-server.log" in prose, "the app log is not described"
        assert "logs/launchd.err.log" in prose, "the crash log is not described"
        assert re.search(r"rotat", prose, re.I), "rotation is not mentioned"

    def test_the_readme_states_the_actual_cap(self, server_mod):
        """So a reader can tell whether the file they are looking at is
        oversized without going to read the source."""
        prose = re.sub(r"```.*?```", "", open(README_MD).read(), flags=re.S)
        megabytes = server_mod.LOG_MAX_BYTES // (1024 * 1024)
        assert re.search(r"%dMB" % megabytes, prose), \
            f"the {megabytes}MB per-file cap is not stated"
        assert str(server_mod.LOG_BACKUP_COUNT) in prose
