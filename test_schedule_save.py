"""Tests for whole-routine save, and for the wall-clock information the
editor renders.

Before this a routine could only be built up across several requests --
schedule_add, then one schedule_step_add per step -- so a partially built
routine was live and armed between calls, and there was no way to change an
existing one at all. Correcting 06:00 to 06:30 meant deleting and rebuilding.
"""
import datetime
import json

import pytest


@pytest.fixture(autouse=True)
def _isolate(server_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "SCHEDULES_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(server_mod, "_schedules", [])
    yield


WAKE = dict(
    time="06:00", days=[0, 1, 2, 3, 4], label="wake-up",
    steps=[
        {"offset": 0, "action": "volume", "volume": 10},
        {"offset": 1, "action": "play", "uri": "spotify:playlist:abc123"},
        {"offset": 75, "action": "pause"},
    ],
)


class TestCreate:
    def test_a_whole_routine_lands_in_one_request(self, save_schedule, server_mod):
        result = save_schedule(**WAKE)
        assert result["status"] == "added"
        assert len(server_mod._schedules) == 1
        assert [s["action"] for s in server_mod._schedules[0]["steps"]] == [
            "volume", "play", "pause"]

    def test_steps_are_ordered_by_offset_not_by_submission(self, save_schedule):
        result = save_schedule(time="06:00", days=[], label="x", steps=[
            {"offset": 30, "action": "pause"},
            {"offset": 0, "action": "resume"},
        ])
        assert [s["offset"] for s in result["schedule"]["steps"]] == [0, 30]

    def test_a_routine_with_no_steps_is_allowed(self, save_schedule):
        """The editor opens empty; saving early must not 400."""
        assert save_schedule(time="06:00", days=[], label="empty", steps=[])["status"] == "added"

    def test_new_routines_are_enabled_unless_told_otherwise(self, save_schedule):
        assert save_schedule(**WAKE)["schedule"]["enabled"] is True
        assert save_schedule(time="07:00", days=[], label="off",
                    enabled=False, steps=[])["schedule"]["enabled"] is False


class TestEdit:
    def test_changing_the_time_keeps_the_same_routine(self, save_schedule, server_mod):
        first = save_schedule(**WAKE)["schedule"]
        second = save_schedule(**{**WAKE, "id": first["id"], "time": "06:30"})

        assert second["status"] == "updated"
        assert len(server_mod._schedules) == 1, "edit must not create a second"
        assert second["schedule"]["id"] == first["id"]
        assert second["schedule"]["time"] == "06:30"

    def test_a_step_can_be_removed_by_saving_without_it(self, save_schedule):
        first = save_schedule(**WAKE)["schedule"]
        result = save_schedule(**{**WAKE, "id": first["id"], "steps": WAKE["steps"][:2]})
        assert len(result["schedule"]["steps"]) == 2

    def test_editing_preserves_position_in_the_list(self, save_schedule, server_mod):
        a = save_schedule(time="06:00", days=[], label="a", steps=[])["schedule"]
        save_schedule(time="07:00", days=[], label="b", steps=[])
        save_schedule(id=a["id"], time="06:00", days=[], label="a2", steps=[])
        assert [e["label"] for e in server_mod._schedules] == ["a2", "b"]

    def test_unknown_id_is_400_not_a_silent_create(self, save_schedule, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            save_schedule(id="sch_nope", time="06:00", days=[], label="x", steps=[])
        assert exc.value.status == 400
        assert server_mod._schedules == []


class TestFiredStampSurvivesAnEdit:
    """Saving during the exact minute a step fires must not let it fire twice."""

    def test_unchanged_steps_keep_their_stamp(self, save_schedule, server_mod):
        first = save_schedule(**WAKE)["schedule"]
        server_mod._schedules[0]["steps"][1]["last_fired"] = "2026-08-03"

        save_schedule(**{**WAKE, "id": first["id"], "label": "renamed"})

        stamped = [s for s in server_mod._schedules[0]["steps"]
                   if s["action"] == "play"][0]
        assert stamped["last_fired"] == "2026-08-03"

    def test_a_changed_step_is_treated_as_new(self, save_schedule, server_mod):
        """A different playlist is a different step and should be free to run."""
        first = save_schedule(**WAKE)["schedule"]
        server_mod._schedules[0]["steps"][1]["last_fired"] = "2026-08-03"

        changed = [dict(s) for s in WAKE["steps"]]
        changed[1] = {**changed[1], "uri": "spotify:playlist:different"}
        save_schedule(**{**WAKE, "id": first["id"], "steps": changed})

        played = [s for s in server_mod._schedules[0]["steps"]
                  if s["action"] == "play"][0]
        assert played["last_fired"] is None

    def test_an_edited_routine_does_not_refire_the_same_minute(self, save_schedule, server_mod):
        """The end-to-end version of the above, through _due_steps."""
        now = datetime.datetime(2026, 8, 3, 6, 0)  # a Monday
        saved = save_schedule(**WAKE)["schedule"]
        assert len(server_mod._due_steps(now.timetuple())) == 1

        save_schedule(**{**WAKE, "id": saved["id"], "label": "renamed mid-fire"})
        assert server_mod._due_steps(now.timetuple()) == []


class TestValidation:
    def test_a_bad_uri_is_rejected_before_anything_is_stored(self, save_schedule, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            save_schedule(time="06:00", days=[], label="x", steps=[
                {"offset": 0, "action": "play", "uri": "../../Bedroom/pause"}])
        assert exc.value.status == 400
        assert server_mod._schedules == [], "a rejected save must leave nothing behind"

    def test_one_bad_step_rejects_the_whole_routine(self, save_schedule, server_mod):
        """Atomicity: no half-built routine goes live."""
        with pytest.raises(server_mod.cherrypy.HTTPError):
            save_schedule(time="06:00", days=[], label="x", steps=[
                {"offset": 0, "action": "volume", "volume": 10},
                {"offset": 5, "action": "not-a-real-action"},
            ])
        assert server_mod._schedules == []

    @pytest.mark.parametrize("bad", ["25:00", "6:00", "", None, "aa:bb"])
    def test_bad_times_rejected(self, save_schedule, server_mod, bad):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            save_schedule(time=bad, days=[], label="x", steps=[])

    def test_step_cap_is_enforced(self, save_schedule, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "MAX_STEPS", 2)
        with pytest.raises(server_mod.cherrypy.HTTPError):
            save_schedule(time="06:00", days=[], label="x",
                 steps=[{"offset": i, "action": "skip"} for i in range(3)])

    def test_schedule_cap_is_enforced(self, save_schedule, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "MAX_SCHEDULES", 1)
        save_schedule(time="06:00", days=[], label="a", steps=[])
        with pytest.raises(server_mod.cherrypy.HTTPError):
            save_schedule(time="07:00", days=[], label="b", steps=[])

    def test_editing_at_the_cap_is_still_allowed(self, save_schedule, server_mod, monkeypatch):
        """Replacing a routine adds nothing, so the cap must not block it."""
        a = save_schedule(time="06:00", days=[], label="a", steps=[])["schedule"]
        monkeypatch.setattr(server_mod, "MAX_SCHEDULES", 1)
        assert save_schedule(id=a["id"], time="06:30", days=[], label="a",
                    steps=[])["status"] == "updated"

    def test_a_non_object_body_is_400(self, dj, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod.cherrypy.request, "json",
                            ["not", "an", "object"], raising=False)
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.schedule_save()
        assert exc.value.status == 400

    def test_steps_must_be_a_list(self, save_schedule, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            save_schedule(time="06:00", days=[], label="x", steps={"offset": 0})


class TestEndpointDiscipline:
    def test_save_is_post_only(self, server_mod):
        conf = server_mod.DJServer.schedule_save._cp_config
        assert conf.get("tools.allow.methods") == ["POST"]

    def test_save_is_not_public(self, server_mod):
        assert "/schedule_save" not in server_mod.PUBLIC_PATHS


class TestNextRun:
    """The information whose absence hid a Sunday-only wake-up alarm."""

    MONDAY_NOON = datetime.datetime(2026, 8, 3, 12, 0)

    def test_later_today_when_today_matches(self, server_mod):
        entry = {"time": "18:00", "days": [0], "enabled": True}
        assert server_mod._next_run(entry, self.MONDAY_NOON) == "2026-08-03T18:00"

    def test_next_week_when_todays_time_has_passed(self, server_mod):
        entry = {"time": "06:00", "days": [0], "enabled": True}
        assert server_mod._next_run(entry, self.MONDAY_NOON) == "2026-08-10T06:00"

    def test_the_sunday_only_case_reads_as_sunday(self, server_mod):
        """The live routine that silently ran one day a week."""
        entry = {"time": "06:00", "days": [6], "enabled": True}
        assert server_mod._next_run(entry, self.MONDAY_NOON) == "2026-08-09T06:00"

    def test_weekdays_from_a_friday_evening_is_monday(self, server_mod):
        entry = {"time": "06:00", "days": [0, 1, 2, 3, 4], "enabled": True}
        friday = datetime.datetime(2026, 8, 7, 21, 0)
        assert server_mod._next_run(entry, friday) == "2026-08-10T06:00"

    def test_empty_days_means_every_day(self, server_mod):
        entry = {"time": "18:00", "days": [], "enabled": True}
        assert server_mod._next_run(entry, self.MONDAY_NOON) == "2026-08-03T18:00"

    def test_a_disabled_routine_has_no_next_run(self, server_mod):
        entry = {"time": "18:00", "days": [], "enabled": False}
        assert server_mod._next_run(entry, self.MONDAY_NOON) is None

    def test_a_malformed_time_has_no_next_run(self, server_mod):
        assert server_mod._next_run({"time": "nope", "enabled": True}) is None

    def test_it_is_exposed_on_the_listing(self, save_schedule, dj):
        save_schedule(**WAKE)
        assert dj.schedules()["schedules"][0]["next_run"]


class TestStepsCarryTheirClockTime:
    """So the editor shows 07:15 rather than +75m, using the same arithmetic
    that decides when the step actually fires."""

    def test_offsets_become_wall_clock_times(self, save_schedule, dj):
        save_schedule(**WAKE)
        steps = dj.schedules()["schedules"][0]["steps"]
        assert [s["at"] for s in steps] == ["06:00", "06:01", "07:15"]

    def test_a_step_past_midnight_is_flagged(self, save_schedule, dj):
        save_schedule(time="23:30", days=[], label="late", steps=[
            {"offset": 0, "action": "resume"},
            {"offset": 60, "action": "pause"},
        ])
        steps = dj.schedules()["schedules"][0]["steps"]
        assert [(s["at"], s["next_day"]) for s in steps] == [
            ("23:30", False), ("00:30", True)]

    def test_the_stored_offset_is_untouched(self, save_schedule, dj, server_mod):
        """Annotation is for display; it must not rewrite what fires."""
        save_schedule(**WAKE)
        assert [s["offset"] for s in server_mod._schedules[0]["steps"]] == [0, 1, 75]
        assert "at" not in server_mod._schedules[0]["steps"][0]
