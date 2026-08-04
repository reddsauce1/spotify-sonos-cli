"""Tests for the counters and /metrics.

The log answers "what happened at 07:00". These answer "is this getting
worse", which grepping a rotating file does badly. The properties worth
holding: every Sonos call is counted exactly once whether it worked or not,
and a single 46-second playlist does not get to swallow the transport average.
"""
import time
from unittest.mock import MagicMock, patch

import pytest
import requests


def _ok_response(payload=None):
    response = MagicMock(status_code=200)
    response.json.return_value = {"ok": True} if payload is None else payload
    return response


class TestCounting:
    def test_a_successful_call_is_counted(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_ok_response()):
            dj._sonos_request("pause")
        assert server_mod._metrics["sonos_calls"] == 1
        assert server_mod._metrics["sonos_failures"] == 0

    def test_a_failed_call_is_counted_as_both(self, dj, server_mod):
        """A failure is still a call -- otherwise the failure rate is wrong in
        the direction that hides an outage."""
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.Timeout("slow")):
            dj._sonos_request("pause")
        assert server_mod._metrics["sonos_calls"] == 1
        assert server_mod._metrics["sonos_failures"] == 1

    def test_an_http_error_counts_as_a_failure(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=MagicMock(status_code=500)):
            dj._sonos_request("pause")
        assert server_mod._metrics["sonos_failures"] == 1

    def test_content_loads_are_counted_separately(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_ok_response()):
            dj._sonos_request("spotify/queue/spotify:playlist:abc")
        assert server_mod._metrics["content_loads"] == 1
        # Counted in the total, but kept out of the transport timing.
        assert server_mod._metrics["sonos_calls"] == 1
        assert server_mod._metrics["sonos_seconds_total"] == 0.0

    def test_transport_calls_are_not_counted_as_content(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_ok_response()):
            dj._sonos_request("volume/20")
        assert server_mod._metrics["content_loads"] == 0

    @pytest.mark.parametrize("endpoint,is_content", [
        ("spotify/now/spotify:track:a", True),
        ("spotify/queue/spotify:album:b", True),
        ("spotify/next/spotify:playlist:c", True),
        ("pause", False),
        ("state", False),
        ("queuemove/1/2", False),
    ])
    def test_the_content_classification(self, server_mod, endpoint, is_content):
        assert server_mod._is_content_endpoint(endpoint) is is_content


class TestTheEndpoint:
    def _metrics(self, dj, server_mod):
        return dj.metrics()

    def test_it_makes_no_sonos_call(self, dj, server_mod):
        """/metrics reads what already happened. A live call here would make
        the diagnostic endpoint fail in exactly the outage it is meant to
        describe."""
        with patch.object(server_mod.requests, "get") as get:
            dj.metrics()
        get.assert_not_called()

    def test_the_average_excludes_content_loads(self, dj, server_mod):
        """One 46-second playlist must not become the transport average."""
        server_mod._metrics.update({
            'sonos_calls': 3, 'content_loads': 1,
            'sonos_seconds_total': 0.02, 'content_seconds_max': 46.0,
        })
        result = dj.metrics()
        assert result["sonos_seconds_avg"] == 0.01     # 0.02 over 2 transport calls
        assert result["content_seconds_max"] == 46.0

    def test_no_calls_yet_does_not_divide_by_zero(self, dj, server_mod):
        assert dj.metrics()["sonos_seconds_avg"] == 0.0

    def test_it_reports_uptime_so_a_count_is_readable(self, dj, server_mod):
        """The counters reset on restart; a bare number means nothing without
        knowing how long it has been accumulating."""
        assert dj.metrics()["uptime_seconds"] >= 0

    def test_it_carries_the_watchdog_verdict(self, dj, server_mod):
        server_mod._watchdog.update({"ok": False, "outages": 2})
        result = dj.metrics()
        assert result["sonos_ready"] is False
        assert result["sonos_outages"] == 2

    def test_it_is_not_public(self, server_mod):
        assert "/metrics" not in server_mod.PUBLIC_PATHS


class TestScheduleAndChatCounters:
    def test_a_fired_step_counts(self, server_mod):
        server_mod._schedules.append(
            {"id": "s", "time": "07:00", "days": [], "enabled": True,
             "steps": [{"offset": 0, "action": "pause", "last_fired": None}]})
        claimed = server_mod._due_steps(
            time.struct_time((2026, 8, 3, 7, 0, 0, 0, 1, -1)))
        for step in claimed:
            server_mod._record_step_outcome(step, True, None)
        assert server_mod._metrics["schedule_fires"] == 1
        assert server_mod._metrics["schedule_failures"] == 0

    def test_a_failed_step_counts_as_a_failure(self, server_mod):
        server_mod._schedules.append(
            {"id": "s", "time": "07:00", "days": [], "enabled": True,
             "steps": [{"offset": 0, "action": "pause", "last_fired": None}]})
        claimed = server_mod._due_steps(
            time.struct_time((2026, 8, 3, 7, 0, 0, 0, 1, -1)))
        for step in claimed:
            server_mod._record_step_outcome(step, False, "Sonos request timed out")
        assert server_mod._metrics["schedule_fires"] == 0
        assert server_mod._metrics["schedule_failures"] == 1
