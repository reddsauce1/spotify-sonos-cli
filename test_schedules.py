"""Tests for the scheduler.

The failure modes that matter for something firing unattended at 07:00:
firing twice, firing on the wrong day, firing hours late after a restart, one
bad entry stopping every other alarm, and the whole ticker dying silently.
"""
import json
import os
import time
from unittest.mock import patch

import pytest
import requests


def _tm(hour, minute, wday=0, year=2026, mon=8, mday=3):
    """A struct_time with the fields the scheduler reads."""
    return time.struct_time((year, mon, mday, hour, minute, 0, wday, 1, -1))


@pytest.fixture(autouse=True)
def _isolate(server_mod, tmp_path, monkeypatch):
    """Never touch the real schedules.json."""
    monkeypatch.setattr(server_mod, "SCHEDULES_PATH", str(tmp_path / "schedules.json"))
    monkeypatch.setattr(server_mod, "_schedules", [])
    yield


def _add(server_mod, **kw):
    entry = {
        "id": kw.get("id", "sch_test"),
        "time": kw.get("time", "07:00"),
        "action": kw.get("action", "pause"),
        "days": kw.get("days", []),
        "label": kw.get("label", "test"),
        "enabled": kw.get("enabled", True),
        "last_fired": kw.get("last_fired"),
    }
    for extra in ("uri", "volume"):
        if extra in kw:
            entry[extra] = kw[extra]
    server_mod._schedules.append(entry)
    return entry


class TestDueDetection:
    def test_fires_at_its_minute(self, server_mod):
        _add(server_mod, time="07:00")
        assert len(server_mod._due_schedules(_tm(7, 0))) == 1

    def test_does_not_fire_a_minute_early_or_late(self, server_mod):
        _add(server_mod, time="07:00")
        assert server_mod._due_schedules(_tm(6, 59)) == []
        assert server_mod._due_schedules(_tm(7, 1)) == []

    def test_does_not_fire_twice_in_the_same_minute(self, server_mod):
        """The tick is shorter than a minute, so the same minute is seen more
        than once -- an unclaimed schedule would fire on every tick."""
        _add(server_mod, time="07:00")
        assert len(server_mod._due_schedules(_tm(7, 0))) == 1
        assert server_mod._due_schedules(_tm(7, 0)) == []

    def test_a_restart_hours_later_does_not_blast(self, server_mod):
        """The reason to match on the exact minute: a server that comes back at
        09:30 must not fire the 07:00 alarm."""
        _add(server_mod, time="07:00")
        assert server_mod._due_schedules(_tm(9, 30)) == []

    def test_fires_again_the_next_day(self, server_mod):
        _add(server_mod, time="07:00")
        assert len(server_mod._due_schedules(_tm(7, 0, mday=3))) == 1
        assert len(server_mod._due_schedules(_tm(7, 0, mday=4))) == 1

    def test_disabled_never_fires(self, server_mod):
        _add(server_mod, time="07:00", enabled=False)
        assert server_mod._due_schedules(_tm(7, 0)) == []


class TestDays:
    def test_empty_days_means_every_day(self, server_mod):
        _add(server_mod, time="07:00", days=[])
        for wday in range(7):
            server_mod._schedules[0]["last_fired"] = None
            assert len(server_mod._due_schedules(_tm(7, 0, wday=wday, mday=3 + wday))) == 1

    def test_weekdays_only_skips_the_weekend(self, server_mod):
        _add(server_mod, time="07:00", days=[0, 1, 2, 3, 4])
        # 5 = Saturday, 6 = Sunday
        assert server_mod._due_schedules(_tm(7, 0, wday=5)) == []
        assert server_mod._due_schedules(_tm(7, 0, wday=6)) == []
        assert len(server_mod._due_schedules(_tm(7, 0, wday=2))) == 1


class TestFiring:
    def test_play_sets_volume_before_playing(self, dj, server_mod):
        """A morning alarm must not blast at whatever level last night ended
        on, so volume has to be set first."""
        calls = []
        with patch.object(dj, "_do_volume", side_effect=lambda **k: calls.append("volume")):
            with patch.object(dj, "_do_play", side_effect=lambda **k: calls.append("play")):
                server_mod._fire_schedule(dj, {
                    "action": "play", "uri": "spotify:playlist:abc",
                    "volume": 25, "label": "morning",
                })
        assert calls == ["volume", "play"]

    def test_play_without_volume_does_not_touch_it(self, dj, server_mod):
        with patch.object(dj, "_do_volume") as vol:
            with patch.object(dj, "_do_play"):
                server_mod._fire_schedule(dj, {
                    "action": "play", "uri": "spotify:playlist:abc", "label": "x",
                })
        vol.assert_not_called()

    @pytest.mark.parametrize("action,method", [
        ("pause", "_do_pause"), ("resume", "_do_resume"),
        ("skip", "_do_skip"), ("previous", "_do_previous"),
        ("clearqueue", "_do_clearqueue"),
    ])
    def test_each_action_calls_its_handler(self, dj, server_mod, action, method):
        with patch.object(dj, method) as m:
            server_mod._fire_schedule(dj, {"action": action, "label": action})
        m.assert_called_once()

    def test_upstream_failure_is_logged_not_raised(self, dj, server_mod, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="dj"):
            with patch.object(server_mod.requests, "get",
                              side_effect=requests.exceptions.ConnectionError("down")):
                server_mod._fire_schedule(dj, {"action": "pause", "label": "morning"})
        assert any(r.levelno >= logging.ERROR for r in caplog.records)
        assert "morning" in caplog.text

    def test_a_broken_entry_does_not_stop_the_others(self, dj, server_mod):
        """One malformed schedule must not take the whole ticker down.

        run_due_schedules reads the real clock, so the time is pinned to the
        minute both entries are set for.
        """
        _add(server_mod, id="bad", time="07:00", action="nonsense")
        _add(server_mod, id="good", time="07:00", action="pause")
        with patch.object(server_mod.time, "localtime", return_value=_tm(7, 0)):
            with patch.object(dj, "_do_pause") as ok:
                server_mod.run_due_schedules(dj)
        ok.assert_called_once()

    def test_broken_entry_first_still_lets_later_ones_run(self, dj, server_mod):
        """Ordering matters: the bad entry is claimed before the good one."""
        _add(server_mod, id="bad", time="07:00", action="play")  # no uri
        _add(server_mod, id="good", time="07:00", action="skip")
        with patch.object(server_mod.time, "localtime", return_value=_tm(7, 0)):
            with patch.object(dj, "_do_skip") as ok:
                server_mod.run_due_schedules(dj)
        ok.assert_called_once()

    def test_tick_survives_an_unexpected_error(self, dj, server_mod, caplog):
        """If the tick raised, the Monitor thread would die and every future
        alarm would silently stop."""
        import logging
        with patch.object(server_mod, "_due_schedules", side_effect=RuntimeError("boom")):
            with caplog.at_level(logging.INFO, logger="dj"):
                server_mod.run_due_schedules(dj)   # must not raise
        assert "boom" in caplog.text


class TestValidation:
    @pytest.mark.parametrize("bad_time", ["7:00", "25:00", "07:60", "", None, "0700", "seven"])
    def test_bad_times_rejected(self, server_mod, bad_time):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_schedule(bad_time, "pause", "", None, None, "x")
        assert exc.value.status == 400

    def test_unknown_action_rejected(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_schedule("07:00", "launch_missiles", "", None, None, "x")

    def test_play_requires_a_valid_uri(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_schedule("07:00", "play", "", "../../Bedroom/pause", None, "x")

    def test_volume_bounds_enforced(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_schedule("07:00", "volume", "", None, 500, "x")

    def test_bad_day_rejected(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_schedule("07:00", "pause", "0,9", None, None, "x")

    def test_days_are_deduped_and_sorted(self, server_mod):
        entry = server_mod._validate_schedule("07:00", "pause", "3,1,1,0", None, None, "x")
        assert entry["days"] == [0, 1, 3]

    def test_label_is_bounded(self, server_mod):
        entry = server_mod._validate_schedule("07:00", "pause", "", None, None, "z" * 500)
        assert len(entry["label"]) <= 80


class TestPersistence:
    def test_survives_a_round_trip(self, server_mod):
        _add(server_mod, time="07:00", label="morning")
        with server_mod._schedules_lock:
            server_mod._save_schedules_locked()
        assert server_mod._load_schedules()[0]["label"] == "morning"

    def test_missing_file_is_not_fatal(self, server_mod):
        assert server_mod._load_schedules() == []

    def test_corrupt_file_is_not_fatal(self, server_mod, caplog):
        """Losing alarms beats refusing to serve music."""
        import logging
        with open(server_mod.SCHEDULES_PATH, 'w') as f:
            f.write("{ this is not json")
        with caplog.at_level(logging.INFO, logger="dj"):
            assert server_mod._load_schedules() == []
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_non_list_file_is_not_fatal(self, server_mod):
        with open(server_mod.SCHEDULES_PATH, 'w') as f:
            json.dump({"not": "a list"}, f)
        assert server_mod._load_schedules() == []

    def test_write_is_atomic(self, server_mod):
        """A temp file plus rename, so a crash mid-write cannot leave a
        truncated file that reads back as zero schedules."""
        _add(server_mod, time="07:00")
        with server_mod._schedules_lock:
            server_mod._save_schedules_locked()
        assert not os.path.exists(server_mod.SCHEDULES_PATH + '.tmp')
        assert json.load(open(server_mod.SCHEDULES_PATH))


class TestEndpoints:
    def test_mutations_are_post_only(self, server_mod):
        """A GET would put the schedule in the access log and let a stray link
        change the alarm."""
        for name in ('schedule_add', 'schedule_delete', 'schedule_toggle', 'schedule_run'):
            conf = getattr(getattr(server_mod.DJServer, name), '_cp_config', {})
            assert conf.get('tools.allow.methods') == ['POST'], name

    def test_schedules_endpoint_is_not_public(self, server_mod):
        for path in ('/schedules', '/schedule_add', '/schedule_delete',
                     '/schedule_toggle', '/schedule_run'):
            assert path not in server_mod.PUBLIC_PATHS

    def test_add_then_list_then_delete(self, dj, server_mod):
        added = dj.schedule_add(time="07:00", action="pause", label="morning")
        sid = added["schedule"]["id"]
        assert any(s["id"] == sid for s in dj.schedules()["schedules"])
        dj.schedule_delete(id=sid)
        assert not any(s["id"] == sid for s in dj.schedules()["schedules"])

    def test_delete_unknown_id_is_400(self, dj, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.schedule_delete(id="sch_nope")
        assert exc.value.status == 400

    def test_toggle_flips_enabled(self, dj, server_mod):
        sid = dj.schedule_add(time="07:00", action="pause")["schedule"]["id"]
        assert dj.schedule_toggle(id=sid)["schedule"]["enabled"] is False
        assert dj.schedule_toggle(id=sid)["schedule"]["enabled"] is True

    def test_run_now_fires_without_waiting(self, dj, server_mod):
        sid = dj.schedule_add(time="07:00", action="pause")["schedule"]["id"]
        with patch.object(dj, "_do_pause") as m:
            dj.schedule_run(id=sid)
        m.assert_called_once()

    def test_run_now_does_not_consume_the_scheduled_run(self, dj, server_mod):
        """Testing an alarm at lunchtime must not stop it firing tomorrow."""
        sid = dj.schedule_add(time="07:00", action="pause")["schedule"]["id"]
        with patch.object(dj, "_do_pause"):
            dj.schedule_run(id=sid)
        entry = next(s for s in server_mod._schedules if s["id"] == sid)
        assert entry["last_fired"] is None

    def test_cap_on_total_schedules(self, dj, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "MAX_SCHEDULES", 3)
        for _ in range(3):
            dj.schedule_add(time="07:00", action="pause")
        with pytest.raises(server_mod.cherrypy.HTTPError):
            dj.schedule_add(time="07:00", action="pause")


class TestTickFrequency:
    def test_tick_is_under_a_minute(self, server_mod):
        """Schedules match on an exact HH:MM. A tick of 60s or more can step
        straight over a minute and miss it entirely."""
        assert server_mod.SCHEDULE_TICK_SECONDS < 60
