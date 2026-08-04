"""Tests for the scheduler.

The failure modes that matter for something firing unattended at 07:00:
firing twice, firing on the wrong day, firing hours late after a restart, one
bad entry stopping every other alarm, and the whole ticker dying silently.
"""
import datetime
import json
import os
import time
from unittest.mock import patch

import pytest
import requests


# 2026-08-03 is a Monday, so weekday N is that many days later.
_BASE_MONDAY = datetime.date(2026, 8, 3)


def _tm(hour, minute, wday=0, days_later=0):
    """A struct_time whose date genuinely falls on `wday`.

    The scheduler derives the weekday from the date rather than trusting
    tm_wday, so a helper that let the two disagree would test a state the
    real clock can never produce.
    """
    day = _BASE_MONDAY + datetime.timedelta(days=wday + days_later)
    return time.struct_time(
        (day.year, day.month, day.day, hour, minute, 0, day.weekday(), 1, -1)
    )


def _step(action="pause", offset=0, **kw):
    step = {"offset": offset, "action": action, "last_fired": kw.pop("last_fired", None)}
    step.update(kw)
    return step


def _add(server_mod, **kw):
    """Build a routine. `action` is shorthand for a single zero-offset step."""
    steps = kw.get("steps")
    if steps is None:
        steps = [_step(
            action=kw.get("action", "pause"),
            offset=kw.get("offset", 0),
            last_fired=kw.get("last_fired"),
            **{k: kw[k] for k in ("uri", "volume") if k in kw}
        )]
    entry = {
        "id": kw.get("id", "sch_test"),
        "time": kw.get("time", "07:00"),
        "days": kw.get("days", []),
        "label": kw.get("label", "test"),
        "enabled": kw.get("enabled", True),
        "steps": steps,
    }
    server_mod._schedules.append(entry)
    return entry


def _complete(server_mod, claimed, ok=True, error=None):
    """Report claimed steps as finished, the way run_due_schedules does.

    _due_steps only stamps the attempt now; success is committed separately
    and the in-flight claim released. A test that claims without reporting
    leaves the step looking like it is still running, which is exactly what
    blocks a second claim.
    """
    for step in claimed:
        server_mod._record_step_outcome(step, ok, error)
    return claimed


class TestDueDetection:
    def test_fires_at_its_minute(self, server_mod):
        _add(server_mod, time="07:00")
        assert len(server_mod._due_steps(_tm(7, 0))) == 1

    def test_does_not_fire_a_minute_early_or_late(self, server_mod):
        _add(server_mod, time="07:00")
        assert server_mod._due_steps(_tm(6, 59)) == []
        assert server_mod._due_steps(_tm(7, 1)) == []

    def test_does_not_fire_twice_in_the_same_minute(self, server_mod):
        """The tick is shorter than a minute, so the same minute is seen more
        than once -- an unclaimed schedule would fire on every tick."""
        _add(server_mod, time="07:00")
        assert len(server_mod._due_steps(_tm(7, 0))) == 1
        assert server_mod._due_steps(_tm(7, 0)) == []

    def test_a_restart_hours_later_does_not_blast(self, server_mod):
        """The reason to match on the exact minute: a server that comes back at
        09:30 must not fire the 07:00 alarm."""
        _add(server_mod, time="07:00")
        assert server_mod._due_steps(_tm(9, 30)) == []

    def test_fires_again_the_next_day(self, server_mod):
        _add(server_mod, time="07:00")
        _complete(server_mod, server_mod._due_steps(_tm(7, 0)))
        assert len(server_mod._due_steps(_tm(7, 0, days_later=1))) == 1

    def test_disabled_never_fires(self, server_mod):
        _add(server_mod, time="07:00", enabled=False)
        assert server_mod._due_steps(_tm(7, 0)) == []


class TestDays:
    def test_empty_days_means_every_day(self, server_mod):
        _add(server_mod, time="07:00", days=[])
        for wday in range(7):
            due = server_mod._due_steps(_tm(7, 0, wday=wday))
            assert len(due) == 1
            _complete(server_mod, due)

    def test_weekdays_only_skips_the_weekend(self, server_mod):
        _add(server_mod, time="07:00", days=[0, 1, 2, 3, 4])
        # 5 = Saturday, 6 = Sunday
        assert server_mod._due_steps(_tm(7, 0, wday=5)) == []
        assert server_mod._due_steps(_tm(7, 0, wday=6)) == []
        assert len(server_mod._due_steps(_tm(7, 0, wday=2))) == 1


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
        with patch.object(server_mod, "_due_steps", side_effect=RuntimeError("boom")):
            with caplog.at_level(logging.INFO, logger="dj"):
                server_mod.run_due_schedules(dj)   # must not raise
        assert "boom" in caplog.text


class TestValidation:
    @pytest.mark.parametrize("bad_time", ["7:00", "25:00", "07:60", "", None, "0700", "seven"])
    def test_bad_times_rejected(self, server_mod, bad_time):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            server_mod._validate_schedule(bad_time, "", "x")
        assert exc.value.status == 400

    def test_unknown_action_rejected(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_step("launch_missiles")

    def test_play_requires_a_valid_uri(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_step("play", 0, "../../Bedroom/pause", None)

    def test_volume_bounds_enforced(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_step("volume", 0, None, 500)

    def test_bad_day_rejected(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_schedule("07:00", "0,9", "x")

    def test_days_are_deduped_and_sorted(self, server_mod):
        entry = server_mod._validate_schedule("07:00", "3,1,1,0", "x")
        assert entry["days"] == [0, 1, 3]

    def test_label_is_bounded(self, server_mod):
        entry = server_mod._validate_schedule("07:00", "", "z" * 500)
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


PAUSE_AT_7 = dict(time="07:00", days=[], label="morning",
                  steps=[{"offset": 0, "action": "pause"}])


class TestEndpoints:
    def test_mutations_are_post_only(self, server_mod):
        """A GET would put the schedule in the access log and let a stray link
        change the alarm."""
        for name in ('schedule_save', 'schedule_delete', 'schedule_toggle', 'schedule_run'):
            conf = getattr(getattr(server_mod.DJServer, name), '_cp_config', {})
            assert conf.get('tools.allow.methods') == ['POST'], name

    def test_schedules_endpoint_is_not_public(self, server_mod):
        for path in ('/schedules', '/schedule_save', '/schedule_delete',
                     '/schedule_toggle', '/schedule_run'):
            assert path not in server_mod.PUBLIC_PATHS

    def test_the_superseded_incremental_endpoints_are_gone(self, server_mod):
        """Building a routine across several requests left a half-built one
        live and armed in between, and offered no way to edit it afterwards."""
        for name in ('schedule_add', 'schedule_step_add', 'schedule_step_delete'):
            assert not hasattr(server_mod.DJServer, name), name

    def test_add_then_list_then_delete(self, dj, save_schedule):
        sid = save_schedule(**PAUSE_AT_7)["schedule"]["id"]
        assert any(s["id"] == sid for s in dj.schedules()["schedules"])
        dj.schedule_delete(id=sid)
        assert not any(s["id"] == sid for s in dj.schedules()["schedules"])

    def test_delete_unknown_id_is_400(self, dj, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.schedule_delete(id="sch_nope")
        assert exc.value.status == 400

    def test_toggle_flips_enabled(self, dj, save_schedule):
        sid = save_schedule(**PAUSE_AT_7)["schedule"]["id"]
        assert dj.schedule_toggle(id=sid)["schedule"]["enabled"] is False
        assert dj.schedule_toggle(id=sid)["schedule"]["enabled"] is True

    def test_run_now_fires_without_waiting(self, dj, save_schedule):
        sid = save_schedule(**PAUSE_AT_7)["schedule"]["id"]
        with patch.object(dj, "_do_pause") as m:
            dj.schedule_run(id=sid)
        m.assert_called_once()

    def test_run_now_does_not_consume_the_scheduled_run(self, dj, server_mod, save_schedule):
        """Testing an alarm at lunchtime must not stop it firing tomorrow."""
        sid = save_schedule(**PAUSE_AT_7)["schedule"]["id"]
        with patch.object(dj, "_do_pause"):
            dj.schedule_run(id=sid)
        entry = next(s for s in server_mod._schedules if s["id"] == sid)
        assert all(step["last_fired"] is None for step in entry["steps"])

    def test_cap_on_total_schedules(self, server_mod, monkeypatch, save_schedule):
        monkeypatch.setattr(server_mod, "MAX_SCHEDULES", 3)
        for _ in range(3):
            save_schedule(**PAUSE_AT_7)
        with pytest.raises(server_mod.cherrypy.HTTPError):
            save_schedule(**PAUSE_AT_7)


class TestTickFrequency:
    def test_tick_is_under_a_minute(self, server_mod):
        """Schedules match on an exact HH:MM. A tick of 60s or more can step
        straight over a minute and miss it entirely."""
        assert server_mod.SCHEDULE_TICK_SECONDS < 60


class TestMultiStepRoutines:
    """A fade-in wake-up: volume + play at +0, then rising volume."""

    FADE = [
        {"offset": 0,  "action": "volume", "volume": 12, "last_fired": None},
        {"offset": 0,  "action": "play", "uri": "spotify:playlist:abc", "last_fired": None},
        {"offset": 10, "action": "volume", "volume": 22, "last_fired": None},
        {"offset": 60, "action": "pause", "last_fired": None},
    ]

    def _routine(self, server_mod):
        return _add(server_mod, time="07:00", label="Wake-up",
                    steps=[dict(s) for s in self.FADE])

    def test_only_zero_offset_steps_fire_at_the_trigger(self, server_mod):
        self._routine(server_mod)
        due = server_mod._due_steps(_tm(7, 0))
        assert [s["action"] for s in due] == ["volume", "play"]

    def test_later_step_fires_at_its_own_minute(self, server_mod):
        self._routine(server_mod)
        server_mod._due_steps(_tm(7, 0))
        due = server_mod._due_steps(_tm(7, 10))
        assert len(due) == 1
        assert due[0]["volume"] == 22

    def test_nothing_fires_between_steps(self, server_mod):
        self._routine(server_mod)
        server_mod._due_steps(_tm(7, 0))
        assert server_mod._due_steps(_tm(7, 5)) == []

    def test_a_restart_between_steps_still_runs_the_rest(self, server_mod):
        """Steps are matched against the clock, not run by a sleeping thread,
        so replacing the process mid-routine loses nothing."""
        self._routine(server_mod)
        server_mod._due_steps(_tm(7, 0))

        # Simulate a restart: reload from disk, discarding in-memory state.
        with server_mod._schedules_lock:
            server_mod._save_schedules_locked()
        reloaded = server_mod._load_schedules()
        server_mod._schedules[:] = reloaded

        due = server_mod._due_steps(_tm(8, 0))
        assert [s["action"] for s in due] == ["pause"]

    def test_each_step_is_claimed_independently(self, server_mod):
        self._routine(server_mod)
        server_mod._due_steps(_tm(7, 0))
        assert server_mod._due_steps(_tm(7, 0)) == []
        assert len(server_mod._due_steps(_tm(7, 10))) == 1

    def test_whole_routine_repeats_the_next_day(self, server_mod):
        self._routine(server_mod)
        first = _complete(server_mod, server_mod._due_steps(_tm(7, 0)))
        assert len(first) == 2
        assert len(server_mod._due_steps(_tm(7, 0, days_later=1))) == 2

    def test_label_travels_with_every_step(self, server_mod):
        self._routine(server_mod)
        for step in server_mod._due_steps(_tm(7, 0)):
            assert step["label"] == "Wake-up"


class TestOffsetPastMidnight:
    """A wind-down that starts at 23:50 and ends after midnight."""

    def _routine(self, server_mod, days):
        return _add(server_mod, time="23:50", days=days, label="Wind down", steps=[
            {"offset": 0, "action": "volume", "volume": 8, "last_fired": None},
            {"offset": 30, "action": "pause", "last_fired": None},
        ])

    def test_step_fires_on_the_following_day(self, server_mod):
        self._routine(server_mod, days=[])
        assert len(server_mod._due_steps(_tm(23, 50))) == 1
        assert len(server_mod._due_steps(_tm(0, 20, days_later=1))) == 1

    def test_wrapped_step_follows_the_trigger_day_not_the_landing_day(self, server_mod):
        """A Friday-only routine's 00:20 step belongs to Friday's run, even
        though it lands on Saturday."""
        self._routine(server_mod, days=[4])            # Friday only
        assert len(server_mod._due_steps(_tm(23, 50, wday=4))) == 1
        # Saturday 00:20 -- trigger day was Friday, so it must run.
        assert len(server_mod._due_steps(_tm(0, 20, wday=4, days_later=1))) == 1

    def test_wrapped_step_does_not_fire_for_a_day_the_routine_excludes(self, server_mod):
        self._routine(server_mod, days=[4])            # Friday only
        # Sunday 00:20 -- the trigger day would have been Saturday, excluded.
        assert server_mod._due_steps(_tm(0, 20, wday=6)) == []

    @pytest.mark.parametrize("trigger,offset,expected", [
        ("07:00", 0, ("07:00", 0)),
        ("07:00", 10, ("07:10", 0)),
        ("23:50", 30, ("00:20", 1)),
        ("23:00", 120, ("01:00", 1)),
        ("00:00", 0, ("00:00", 0)),
    ])
    def test_fire_time_arithmetic(self, server_mod, trigger, offset, expected):
        assert server_mod._step_fire_time(trigger, offset) == expected


class TestSteps:
    """Composition of steps is covered in test_schedule_save.py, which owns
    the endpoint. What stays here is the step validator and the behaviour of
    running a routine on demand."""

    def test_offset_is_bounded(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            server_mod._validate_step("pause", 99999)

    def test_run_now_runs_every_step_ignoring_offsets(self, dj, save_schedule):
        """Nobody wants to sit through a 60-minute fade to test it."""
        sid = save_schedule(time="07:00", days=[], label="x", steps=[
            {"offset": 0, "action": "pause"},
            {"offset": 60, "action": "skip"},
        ])["schedule"]["id"]
        with patch.object(dj, "_do_pause") as p, patch.object(dj, "_do_skip") as s:
            result = dj.schedule_run(id=sid)
        p.assert_called_once()
        s.assert_called_once()
        assert result["steps"] == 2


class TestMigrationFromFlatFormat:
    """The first version stored one action per schedule, flat."""

    OLD = {
        "id": "sch_old", "time": "07:15", "action": "pause",
        "days": [0, 1, 2, 3, 4], "label": "Weekday wind-down",
        "enabled": True, "last_fired": "2026-08-01",
    }

    def test_flat_entry_becomes_one_step(self, server_mod):
        migrated = server_mod._migrate_schedule(dict(self.OLD))
        assert migrated["steps"] == [
            {"offset": 0, "action": "pause", "last_fired": "2026-08-01"}
        ]

    def test_routine_fields_are_preserved(self, server_mod):
        migrated = server_mod._migrate_schedule(dict(self.OLD))
        assert migrated["time"] == "07:15"
        assert migrated["days"] == [0, 1, 2, 3, 4]
        assert migrated["label"] == "Weekday wind-down"

    def test_flat_action_fields_do_not_linger(self, server_mod):
        migrated = server_mod._migrate_schedule(dict(self.OLD))
        for gone in ("action", "uri", "volume", "last_fired"):
            assert gone not in migrated

    def test_uri_and_volume_move_into_the_step(self, server_mod):
        old = {**self.OLD, "action": "play",
               "uri": "spotify:playlist:abc", "volume": 25}
        step = server_mod._migrate_schedule(old)["steps"][0]
        assert step["uri"] == "spotify:playlist:abc"
        assert step["volume"] == 25

    def test_already_migrated_entry_is_untouched(self, server_mod):
        new = {"id": "x", "time": "07:00", "steps": [{"offset": 0, "action": "skip"}]}
        assert server_mod._migrate_schedule(dict(new))["steps"] == new["steps"]

    def test_migration_happens_on_load(self, server_mod):
        with open(server_mod.SCHEDULES_PATH, 'w') as f:
            json.dump([self.OLD], f)
        loaded = server_mod._load_schedules()
        assert loaded[0]["steps"][0]["action"] == "pause"

    def test_a_migrated_entry_still_fires(self, server_mod):
        with open(server_mod.SCHEDULES_PATH, 'w') as f:
            json.dump([{**self.OLD, "time": "07:00", "last_fired": None}], f)
        server_mod._schedules[:] = server_mod._load_schedules()
        assert len(server_mod._due_steps(_tm(7, 0))) == 1


class TestFailedStepsRetry:
    """A step used to be stamped fired before its Sonos call ran, so one
    failure burned it for the day -- a wake-up that silently did nothing.
    The attempt and the success are now committed separately."""

    def _fail(self, server_mod, claimed):
        return _complete(server_mod, claimed, ok=False, error="Sonos request timed out")

    def test_a_failed_step_is_not_marked_fired(self, server_mod):
        _add(server_mod, time="07:00")
        self._fail(server_mod, server_mod._due_steps(_tm(7, 0)))
        assert server_mod._schedules[0]["steps"][0].get("last_fired") is None

    def test_a_failed_step_is_retried_inside_the_window(self, server_mod):
        _add(server_mod, time="07:00")
        self._fail(server_mod, server_mod._due_steps(_tm(7, 0)))
        # 07:02 no longer matches the trigger minute, so only the retry path
        # can produce this claim.
        assert len(server_mod._due_steps(_tm(7, 2))) == 1

    def test_a_succeeded_step_is_not_retried(self, server_mod):
        _add(server_mod, time="07:00")
        _complete(server_mod, server_mod._due_steps(_tm(7, 0)))
        assert server_mod._due_steps(_tm(7, 2)) == []

    def test_retries_stop_at_the_attempt_cap(self, server_mod):
        _add(server_mod, time="07:00")
        claims = 0
        for minute in range(0, 5):
            due = server_mod._due_steps(_tm(7, minute))
            claims += len(due)
            self._fail(server_mod, due)
        assert claims == server_mod.SCHEDULE_MAX_ATTEMPTS

    def test_the_window_closes(self, server_mod):
        _add(server_mod, time="07:00")
        self._fail(server_mod, server_mod._due_steps(_tm(7, 0)))
        beyond = server_mod.SCHEDULE_RETRY_WINDOW_SECONDS // 60 + 1
        assert server_mod._due_steps(_tm(7, beyond)) == []

    def test_a_retry_that_succeeds_stamps_fired(self, server_mod):
        _add(server_mod, time="07:00")
        self._fail(server_mod, server_mod._due_steps(_tm(7, 0)))
        _complete(server_mod, server_mod._due_steps(_tm(7, 2)))
        step = server_mod._schedules[0]["steps"][0]
        assert step["last_fired"] is not None
        assert "last_error" not in step

    def test_an_in_flight_step_is_not_claimed_again(self, server_mod):
        _add(server_mod, time="07:00")
        assert len(server_mod._due_steps(_tm(7, 0))) == 1   # claimed, never reported
        assert server_mod._due_steps(_tm(7, 0)) == []
        assert server_mod._due_steps(_tm(7, 2)) == []

    def test_the_failure_is_recorded_for_the_ui(self, server_mod):
        _add(server_mod, time="07:00")
        self._fail(server_mod, server_mod._due_steps(_tm(7, 0)))
        error = server_mod._schedules[0]["steps"][0]["last_error"]
        assert error["message"] == "Sonos request timed out"
        assert error["attempts"] == 1
        assert error["final"] is False

    def test_the_last_failure_is_marked_final(self, server_mod):
        _add(server_mod, time="07:00")
        for minute in range(0, 5):
            self._fail(server_mod, server_mod._due_steps(_tm(7, minute)))
        error = server_mod._schedules[0]["steps"][0]["last_error"]
        assert error["attempts"] == server_mod.SCHEDULE_MAX_ATTEMPTS
        assert error["final"] is True

    def test_a_wrapped_step_retries_against_its_own_trigger_date(self, server_mod):
        """The step lands after midnight but belongs to the previous day's
        run, so the retry window has to measure from the landing time."""
        _add(server_mod, time="23:50", steps=[
            {"offset": 20, "action": "pause", "last_fired": None},
        ])
        self._fail(server_mod, server_mod._due_steps(_tm(0, 10, days_later=1)))
        assert len(server_mod._due_steps(_tm(0, 12, days_later=1))) == 1
