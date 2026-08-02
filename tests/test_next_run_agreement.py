"""The scheduler's next-run calculation exists twice, and this checks the two
copies agree.

The server owns it (_next_run) and is authoritative for saved routines. The
editor needs the same answer for a *draft* -- a routine the server has not
seen yet -- so index.html carries a JavaScript copy, nextRunPreview.

Duplication is the price of previewing unsaved state, but silent drift between
the two is exactly the failure this whole feature exists to prevent: a routine
that claims one thing in the editor and fires on another. The weekday
convention is the likeliest place to drift, since JavaScript's getDay() is
Sunday-first while the scheduler is Monday-first.

Skipped when node is unavailable; the pure-Python behaviour of _next_run is
covered by test_schedule_save.py regardless.
"""
import datetime
import json
import re
import shutil
import subprocess

import pytest

from paths import INDEX_HTML


node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node is needed to run the browser copy")

TIMES = ["00:00", "06:00", "06:30", "12:00", "18:45", "23:59"]
DAY_SETS = [[], [0], [6], [0, 1, 2, 3, 4], [5, 6], [0, 1, 2, 3, 4, 5, 6], [2, 4]]
MOMENTS = [
    "2026-08-01T12:00",  # a Saturday
    "2026-08-03T05:59",  # a Monday, one minute before a 06:00 alarm
    "2026-08-03T06:00",  # exactly on it -- must roll to next week, not today
    "2026-08-03T06:01",
    "2026-08-07T21:00",  # Friday evening, so weekdays means Monday
    "2026-08-09T23:59",  # Sunday, one minute before midnight
    "2026-12-31T23:58",  # year boundary
    "2026-02-28T12:00",  # month boundary
]

CASES = [{"time": t, "days": d, "now": n}
         for t in TIMES for d in DAY_SETS for n in MOMENTS]


DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
// Pulled from the shipped file rather than restated here -- a copy in the
// test could pass while the real page was wrong.
const grab = re => { const m = src.match(re); if (!m) throw new Error('not found: ' + re); return m[0]; };
const body = [
  grab(/const HHMM_RE = .*/),
  grab(/function nextRunPreview\(time, days\) \{[\s\S]*?\n  \}/),
].join('\n');
const RealDate = Date;
eval(body);

const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const pad = n => String(n).padStart(2, '0');

console.log(JSON.stringify(cases.map(c => {
  const frozen = new RealDate(c.now);
  global.Date = class extends RealDate {
    constructor(...a) { super(...(a.length ? a : [frozen])); }
  };
  let out = null;
  try {
    const when = nextRunPreview(c.time, c.days);
    if (when) {
      out = when.getFullYear() + '-' + pad(when.getMonth() + 1) + '-' +
            pad(when.getDate()) + 'T' + pad(when.getHours()) + ':' +
            pad(when.getMinutes());
    }
  } finally {
    global.Date = RealDate;
  }
  return out;
})));
"""


@pytest.fixture(scope="module")
def browser_answers(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node unavailable")
    tmp = tmp_path_factory.mktemp("nextrun")

    markup = open(INDEX_HTML).read()
    script = tmp / "app.js"
    script.write_text(markup.split("<script>", 1)[1].split("</script>", 1)[0])

    driver = tmp / "driver.js"
    driver.write_text(DRIVER)
    cases = tmp / "cases.json"
    cases.write_text(json.dumps(CASES))

    done = subprocess.run(["node", str(driver), str(script), str(cases)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def server_answers(server_mod):
    return [server_mod._next_run(
        {"time": c["time"], "days": c["days"], "enabled": True},
        datetime.datetime.fromisoformat(c["now"])) for c in CASES]


@node
def test_every_case_agrees(browser_answers, server_answers):
    disagreements = [
        f"now={c['now']} time={c['time']} days={c['days']}: "
        f"server={s!r} browser={b!r}"
        for c, s, b in zip(CASES, server_answers, browser_answers) if s != b
    ]
    assert not disagreements, "\n".join(disagreements[:12])


@node
def test_the_comparison_is_not_vacuous(browser_answers, server_answers):
    """A driver that silently returned nulls would agree with nothing useful."""
    assert len(browser_answers) == len(CASES)
    assert sum(1 for a in browser_answers if a) > len(CASES) * 0.9


class TestTheSundayOnlyCase:
    """The bug that motivated showing next-run at all: a wake-up alarm whose
    days were [6] fired only on Sundays, and nothing in the UI ever said so."""

    def test_the_server_reads_it_as_sunday(self, server_mod):
        friday = datetime.datetime(2026, 8, 7, 9, 0)
        assert server_mod._next_run(
            {"time": "06:00", "days": [6], "enabled": True}, friday
        ) == "2026-08-09T06:00"

    @node
    def test_the_browser_agrees(self, browser_answers):
        wanted = {"time": "06:00", "days": [6], "now": "2026-08-07T21:00"}
        assert browser_answers[CASES.index(wanted)] == "2026-08-09T06:00"


class TestBothUseMondayFirstWeekdays:
    """JavaScript getDay() is Sunday-first; the scheduler is Monday-first.
    Mixing them up shifts every routine by a day."""

    def test_the_browser_converts(self):
        markup = open(INDEX_HTML).read()
        body = re.search(r"function nextRunPreview\(time, days\) \{.*?\n  \}",
                         markup, re.S).group(0)
        assert "(when.getDay() + 6) % 7" in body

    def test_the_server_uses_pythons_native_monday_first_weekday(self, server_mod):
        """date.weekday() is already Monday=0, so there is nothing to convert
        -- which is why only one side needs the shift."""
        assert datetime.date(2026, 8, 3).weekday() == 0  # a Monday
        monday_only = {"time": "12:00", "days": [0], "enabled": True}
        assert server_mod._next_run(
            monday_only, datetime.datetime(2026, 8, 3, 9, 0)
        ).startswith("2026-08-03")
