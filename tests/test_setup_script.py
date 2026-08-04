"""Tests for scripts/setup-sonos.sh.

The bridge is a separate upstream project, so getting from a clean machine to
a working system means cloning it, installing it, copying this repo's custom
actions in, and pointing its webhook back here. Three of those four fail
loudly. The webhook does not: without it everything still works, the UI just
quietly reverts to polling and being up to ten seconds stale. That silent step
is the reason the script exists, so these check it stays wired up.
"""
import json
import os
import shutil
import stat
import subprocess

import pytest

from paths import SERVER_PY

REPO = os.path.dirname(SERVER_PY)
SCRIPT = os.path.join(REPO, "scripts", "setup-sonos.sh")


@pytest.fixture(scope="module")
def source():
    with open(SCRIPT) as handle:
        return handle.read()


class TestItShips:
    def test_the_script_exists(self):
        assert os.path.isfile(SCRIPT)

    def test_it_is_executable(self):
        assert os.stat(SCRIPT).st_mode & stat.S_IXUSR

    def test_it_is_valid_shell(self):
        out = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr


class TestItCoversEveryManualStep:
    """Each of these was a step someone had to remember."""

    def test_it_clones_upstream(self, source):
        assert "jishi/node-sonos-http-api.git" in source

    def test_it_installs_dependencies(self, source):
        assert "npm install" in source

    def test_it_copies_the_custom_actions(self, source):
        assert "sonos-actions" in source
        assert "lib/actions" in source

    def test_it_writes_the_webhook(self, source):
        assert "/sonos_event" in source
        assert "webhookHeaderName" in source

    def test_the_webhook_uses_the_configured_port(self, source):
        """A server_port override in config.json would otherwise produce a
        webhook pointing at a port nothing listens on."""
        assert "server_port" in source

    def test_it_locks_down_the_credential_file(self, source):
        """settings.json carries the cli_token."""
        assert "0o600" in source or "chmod 600" in source


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestBehaviour:
    """Run it, against a stub clone rather than the real one."""

    @staticmethod
    def _repo(tmp_path, config):
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "sonos-actions").mkdir()
        shutil.copy(SCRIPT, repo / "scripts" / "setup-sonos.sh")
        for action in os.listdir(os.path.join(REPO, "sonos-actions")):
            shutil.copy(os.path.join(REPO, "sonos-actions", action),
                        repo / "sonos-actions" / action)
        (repo / "config.json").write_text(config)
        return repo

    @staticmethod
    def _target(tmp_path):
        target = tmp_path / "bridge"
        (target / "node_modules").mkdir(parents=True)
        (target / "package.json").write_text('{"name": "node-sonos-http-api"}')
        return target

    @staticmethod
    def _run(repo, target):
        return subprocess.run(
            ["bash", str(repo / "scripts" / "setup-sonos.sh"), str(target)],
            capture_output=True, text=True, timeout=120,
        )

    VALID = json.dumps({"client_id": "a", "client_secret": "b", "cli_token": "t" * 64})

    def test_it_installs_the_actions(self, tmp_path):
        repo, target = self._repo(tmp_path, self.VALID), self._target(tmp_path)
        assert self._run(repo, target).returncode == 0
        installed = os.listdir(target / "lib" / "actions")
        assert "queueedit.js" in installed
        assert "relvolume.js" in installed

    def test_it_writes_a_usable_webhook(self, tmp_path):
        repo, target = self._repo(tmp_path, self.VALID), self._target(tmp_path)
        self._run(repo, target)
        settings = json.loads((target / "settings.json").read_text())
        assert settings["webhook"].endswith("/sonos_event")
        assert settings["webhookHeaderName"] == "X-DJ-Token"
        assert settings["webhookHeaderContents"] == "t" * 64

    def test_it_keeps_unrelated_settings(self, tmp_path):
        """settings.json is also where port and announceVolume live. Adding
        three keys must not throw away someone's tuning."""
        repo, target = self._repo(tmp_path, self.VALID), self._target(tmp_path)
        (target / "settings.json").write_text(json.dumps({"announceVolume": 35}))
        self._run(repo, target)
        settings = json.loads((target / "settings.json").read_text())
        assert settings["announceVolume"] == 35
        assert "webhook" in settings

    def test_running_it_twice_changes_nothing(self, tmp_path):
        repo, target = self._repo(tmp_path, self.VALID), self._target(tmp_path)
        self._run(repo, target)
        first = (target / "settings.json").read_text()
        assert self._run(repo, target).returncode == 0
        assert (target / "settings.json").read_text() == first

    def test_the_credential_file_is_not_world_readable(self, tmp_path):
        repo, target = self._repo(tmp_path, self.VALID), self._target(tmp_path)
        self._run(repo, target)
        mode = os.stat(target / "settings.json").st_mode
        assert not mode & stat.S_IRGRP
        assert not mode & stat.S_IROTH

    def test_a_missing_token_stops_before_touching_anything(self, tmp_path):
        """Writing a webhook with an empty credential would produce a bridge
        whose every push is rejected, with nothing in the log to say why."""
        repo = self._repo(tmp_path, json.dumps({"client_id": "a", "client_secret": "b"}))
        target = self._target(tmp_path)
        out = self._run(repo, target)
        assert out.returncode != 0
        assert "cli_token" in out.stderr
        assert not (target / "settings.json").exists()

    def test_a_directory_that_is_not_the_bridge_is_refused(self, tmp_path):
        repo = self._repo(tmp_path, self.VALID)
        target = tmp_path / "somewhere-else"
        target.mkdir()
        out = self._run(repo, target)
        assert out.returncode != 0
        assert "package.json" in out.stderr
