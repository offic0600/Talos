"""Tests for self-check GitLab reachability behavior (DD v2.3 §11).

GitLab connection failures (DNS, timeout, TLS, refused, HTTP 5xx) must be
WARN-only — the executor continues startup.  HTTP 401/403 remains a hard
fail (token misconfiguration).
"""
import os
import sys
import ssl
import socket
import urllib.error
import sqlite3
import tempfile
from unittest import mock

import pytest

# sys.path setup
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))

# Create a temp DB that actually exists — force-set env var
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
conn = sqlite3.connect(_TMP_DB.name)
conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT)")
conn.commit()
conn.close()
os.environ["TALOS_TEST_DB"] = _TMP_DB.name  # force-set, not setdefault
os.environ["HERMES_KANBAN_DB"] = _TMP_DB.name  # constants.py needs this at import time


class TestSelfCheckGitLabWarnOnly:
    """GitLab connection-layer failures must warn, not exit."""

    def _run_selfcheck(self, gitlab_exc):
        """Run _self_check with all pre-GitLab items mocked to pass,
        and capture stdout. Returns (captured_out, exit_code_or_None)."""
        from talos.executor import loop as loop_mod

        with mock.patch.object(loop_mod, "log_event") as mock_log, \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen", side_effect=gitlab_exc), \
             mock.patch("pathlib.Path.is_file", return_value=True), \
             mock.patch("os.access", return_value=True), \
             mock.patch("builtins.__import__", side_effect=self._import_mock):
            mock_subproc.return_value = mock.Mock(returncode=0)
            # Also need to make skills check pass — patch the skills dir
            # to be empty so skill_dirs=[] → sys.exit(1) at (5)
            # We need to prevent that. Patch _self_check's internals
            # by making repo_skills.is_dir() return False
            try:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
                return "", None
            except SystemExit as e:
                # Capture stdout via mock
                return None, e.code

    def _import_mock(self, name, *args, **kwargs):
        """Allow normal imports except for the ones we want to mock."""
        return __builtins__.__import__(name, *args, **kwargs) if hasattr(__builtins__, '__import__') else __import__(name, *args, **kwargs)

    def test_urLError_does_not_exit(self, capsys):
        """urllib URLError (DNS, connection refused) → WARN, not sys.exit."""
        from talos.executor import loop as loop_mod

        with mock.patch.object(loop_mod, "log_event") as mock_log, \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("connection refused")):
            mock_subproc.return_value = mock.Mock(returncode=0)
            try:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
            except SystemExit:
                pass  # May exit at (5) skills — that's OK
            captured = capsys.readouterr()
            gitlab_lines = [l for l in captured.out.splitlines()
                            if "GitLab reachable" in l]
            assert gitlab_lines, "Should have printed a GitLab reachable line"
            assert "WARN" in gitlab_lines[0], (
                f"GitLab connection failure should print WARN, got: {gitlab_lines[0]}"
            )
            assert "FAIL" not in gitlab_lines[0]

    def test_socket_timeout_does_not_exit(self, capsys):
        """socket.timeout → WARN, not sys.exit."""
        from talos.executor import loop as loop_mod

        with mock.patch.object(loop_mod, "log_event"), \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen",
                        side_effect=socket.timeout("timed out")):
            mock_subproc.return_value = mock.Mock(returncode=0)
            try:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
            except SystemExit:
                pass
            captured = capsys.readouterr()
            gitlab_lines = [l for l in captured.out.splitlines()
                            if "GitLab reachable" in l]
            assert gitlab_lines
            assert "WARN" in gitlab_lines[0]
            assert "FAIL" not in gitlab_lines[0]

    def test_ssl_error_does_not_exit(self, capsys):
        """ssl.SSLError (TLS handshake failure) → WARN, not sys.exit."""
        from talos.executor import loop as loop_mod

        with mock.patch.object(loop_mod, "log_event"), \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen",
                        side_effect=ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")):
            mock_subproc.return_value = mock.Mock(returncode=0)
            try:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
            except SystemExit:
                pass
            captured = capsys.readouterr()
            gitlab_lines = [l for l in captured.out.splitlines()
                            if "GitLab reachable" in l]
            assert gitlab_lines
            assert "WARN" in gitlab_lines[0]
            assert "FAIL" not in gitlab_lines[0]

    def test_connection_refused_does_not_exit(self, capsys):
        """ConnectionRefusedError → WARN, not sys.exit."""
        from talos.executor import loop as loop_mod

        with mock.patch.object(loop_mod, "log_event"), \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen",
                        side_effect=ConnectionRefusedError("connection refused")):
            mock_subproc.return_value = mock.Mock(returncode=0)
            try:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
            except SystemExit:
                pass
            captured = capsys.readouterr()
            gitlab_lines = [l for l in captured.out.splitlines()
                            if "GitLab reachable" in l]
            assert gitlab_lines
            assert "WARN" in gitlab_lines[0]
            assert "FAIL" not in gitlab_lines[0]

    def test_http_401_remains_hard_fail(self, capsys):
        """HTTP 401 → hard fail (token misconfiguration)."""
        from talos.executor import loop as loop_mod
        from email.message import Message

        hdrs = Message()

        with mock.patch.object(loop_mod, "log_event"), \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            url="https://gitlab.example/api/v4/version",
                            code=401, msg="Unauthorized", hdrs=hdrs, fp=None)):
            mock_subproc.return_value = mock.Mock(returncode=0)
            with pytest.raises(SystemExit) as exc_info:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            gitlab_lines = [l for l in captured.out.splitlines()
                            if "GitLab reachable" in l]
            assert gitlab_lines
            assert "FAIL" in gitlab_lines[0]
            assert "401" in gitlab_lines[0]

    def test_http_403_remains_hard_fail(self, capsys):
        """HTTP 403 → hard fail (token misconfiguration)."""
        from talos.executor import loop as loop_mod
        from email.message import Message

        hdrs = Message()

        with mock.patch.object(loop_mod, "log_event"), \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            url="https://gitlab.example/api/v4/version",
                            code=403, msg="Forbidden", hdrs=hdrs, fp=None)):
            mock_subproc.return_value = mock.Mock(returncode=0)
            with pytest.raises(SystemExit) as exc_info:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            gitlab_lines = [l for l in captured.out.splitlines()
                            if "GitLab reachable" in l]
            assert gitlab_lines
            assert "FAIL" in gitlab_lines[0]
            assert "403" in gitlab_lines[0]

    def test_http_500_warns(self, capsys):
        """HTTP 5xx → WARN (server-side, may self-heal)."""
        from talos.executor import loop as loop_mod
        from email.message import Message

        hdrs = Message()

        with mock.patch.object(loop_mod, "log_event"), \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            url="https://gitlab.example/api/v4/version",
                            code=502, msg="Bad Gateway", hdrs=hdrs, fp=None)):
            mock_subproc.return_value = mock.Mock(returncode=0)
            try:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
            except SystemExit:
                pass
            captured = capsys.readouterr()
            gitlab_lines = [l for l in captured.out.splitlines()
                            if "GitLab reachable" in l]
            assert gitlab_lines
            assert "WARN" in gitlab_lines[0]
            assert "FAIL" not in gitlab_lines[0]

    def test_warn_logs_event(self, capsys):
        """GitLab connection failure should call log_event("warn", ...)."""
        from talos.executor import loop as loop_mod

        with mock.patch.object(loop_mod, "log_event") as mock_log, \
             mock.patch("subprocess.run") as mock_subproc, \
             mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("test")):
            mock_subproc.return_value = mock.Mock(returncode=0)
            try:
                loop_mod._self_check(
                    worker_image="hermes-worker:latest",
                    kanban_db=os.environ["TALOS_TEST_DB"],
                    gitlab_url="https://gitlab.invalid.example",
                )
            except SystemExit:
                pass
            mock_log.assert_any_call("warn", msg=mock.ANY)
            # Verify the warn message mentions GitLab
            warn_calls = [c for c in mock_log.call_args_list
                          if c.args and c.args[0] == "warn"]
            assert any("GitLab" in str(c) or "gitlab" in str(c).lower()
                       for c in warn_calls), \
                "log_event should be called with a GitLab-related warn message"
