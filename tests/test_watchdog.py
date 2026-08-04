"""Tests for the readiness watchdog.

launchd's KeepAlive only sees the process. The failure this covers is the one
that actually happened: node-sonos-http-api alive and answering, but with no
system discovered, so every playback call fails while everything looks fine.

The behaviour that matters is the alerting discipline. A watchdog that cries
on every tick is a watchdog you turn off, and one that dies silently is worse
than none because its silence reads as health.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest
import requests


def _zones(payload=None, status_code=200):
    response = MagicMock(status_code=status_code)
    response.json.return_value = (
        [{"coordinator": {"roomName": "Dining Room"}}] if payload is None else payload
    )
    return response


class TestReadiness:
    """One rule, shared by /health and the watchdog."""

    def test_a_zone_list_is_ready(self, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_zones()):
            assert server_mod._sonos_readiness() == (True, "ok")

    def test_an_empty_zone_list_is_not_ready(self, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_zones(payload=[])):
            ok, detail = server_mod._sonos_readiness()
        assert ok is False
        assert detail == "error: no zones discovered"

    def test_a_500_is_not_ready(self, server_mod):
        """The shape of this morning's outage."""
        with patch.object(server_mod.requests, "get", return_value=_zones(status_code=500)):
            ok, detail = server_mod._sonos_readiness()
        assert ok is False
        assert "500" in detail

    def test_unreachable_is_not_ready(self, server_mod):
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("refused")):
            ok, detail = server_mod._sonos_readiness()
        assert ok is False
        assert detail == "error: ConnectionError"

    def test_the_error_does_not_leak_the_exception_message(self, server_mod):
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("secret-host refused")):
            _, detail = server_mod._sonos_readiness()
        assert "secret-host" not in detail

    def test_health_and_the_watchdog_share_it(self, dj, server_mod):
        """Two copies of this rule would be free to drift."""
        with patch.object(server_mod, "_sonos_readiness",
                          return_value=(False, "error: no zones discovered")) as shared:
            with patch.object(server_mod, "sp"):
                result = dj.health()
        assert result["sonos"] == "error: no zones discovered"
        shared.assert_called()


class TestAlerting:
    def _tick(self, server_mod, ready, times=1):
        value = (True, "ok") if ready else (False, "error: no zones discovered")
        with patch.object(server_mod, "_sonos_readiness", return_value=value):
            with patch.object(server_mod, "_notify") as notify:
                for _ in range(times):
                    server_mod.check_sonos_readiness()
        return notify

    def test_one_blip_does_not_alert(self, server_mod):
        """Sonos is on wifi; a single missed check is not an outage."""
        notify = self._tick(server_mod, ready=False, times=1)
        notify.assert_not_called()

    def test_sustained_failure_alerts(self, server_mod):
        notify = self._tick(server_mod, ready=False,
                            times=server_mod.WATCHDOG_FAILURES_BEFORE_ALERT)
        notify.assert_called_once()

    def test_it_alerts_once_per_outage_not_once_per_tick(self, server_mod):
        """60 notifications an hour trains you to ignore them."""
        notify = self._tick(server_mod, ready=False,
                            times=server_mod.WATCHDOG_FAILURES_BEFORE_ALERT + 8)
        assert notify.call_count == 1

    def test_recovery_is_announced(self, server_mod):
        self._tick(server_mod, ready=False,
                   times=server_mod.WATCHDOG_FAILURES_BEFORE_ALERT)
        notify = self._tick(server_mod, ready=True)
        notify.assert_called_once()
        assert "back" in notify.call_args[0][1].lower()

    def test_a_second_outage_alerts_again(self, server_mod):
        self._tick(server_mod, ready=False, times=server_mod.WATCHDOG_FAILURES_BEFORE_ALERT)
        self._tick(server_mod, ready=True)
        notify = self._tick(server_mod, ready=False,
                            times=server_mod.WATCHDOG_FAILURES_BEFORE_ALERT)
        notify.assert_called_once()
        assert server_mod._watchdog["outages"] == 2

    def test_a_healthy_boot_is_not_an_alert(self, server_mod):
        notify = self._tick(server_mod, ready=True, times=3)
        notify.assert_not_called()

    def test_the_failure_is_logged_at_error(self, server_mod, caplog):
        with caplog.at_level(logging.INFO, logger="dj"):
            self._tick(server_mod, ready=False,
                       times=server_mod.WATCHDOG_FAILURES_BEFORE_ALERT)
        assert any(r.levelno >= logging.ERROR for r in caplog.records)


class TestTheTickCannotDie:
    def test_an_unexpected_error_does_not_escape(self, server_mod, caplog):
        """A raising Monitor thread dies silently, and a dead watchdog looks
        exactly like a healthy one."""
        with caplog.at_level(logging.INFO, logger="dj"):
            with patch.object(server_mod, "_sonos_readiness",
                              side_effect=RuntimeError("boom")):
                server_mod.check_sonos_readiness()   # must not raise
        assert "Watchdog tick failed" in caplog.text

    def test_a_failing_notification_does_not_break_the_tick(self, server_mod):
        with patch.object(server_mod.subprocess, "run", side_effect=OSError("no osascript")):
            with patch.object(server_mod, "WATCHDOG_NOTIFY", True):
                server_mod._notify("t", "m")   # must not raise

    def test_notification_is_suppressed_when_switched_off(self, server_mod):
        with patch.object(server_mod, "WATCHDOG_NOTIFY", False):
            with patch.object(server_mod.subprocess, "run") as run:
                server_mod._notify("t", "m")
        run.assert_not_called()
