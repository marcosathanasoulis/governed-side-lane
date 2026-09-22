"""Tests for the shared bounded worker lifecycle ``claude._bounded_process``.

``_bounded_process`` is the one lifecycle the adapters bound a worker
through: the Claude adapter uses it as its default runner and the Codex
adapter substitutes it for ``subprocess.run`` when a route sets
``timeout_seconds``. Its stop path is platform-scoped by
``_posix_process_groups`` — SIGTERM-then-SIGKILL of the worker's process
group where POSIX process groups exist, and elsewhere the OS-native
process-tree stop where one exists (Windows ``taskkill /T /F``) with the
``Popen`` terminate/kill pair as fallback. Every drain is bounded: a
descendant that inherited the output handles must not be able to keep the
runner waiting past the deadline. Every test here is portable: platform
branches are selected through the predicates so both sides run on any
host, and the real fixtures use ``sys.executable`` so they also run on
Windows CI.
"""

import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from side_lane.adapters import claude

# signal.SIGKILL does not exist on Windows; the POSIX branch is exercised
# there through mocks that also create the missing attributes.
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)


def _fake_process(*communicate_results: object, pid: int = 4242) -> mock.Mock:
    process = mock.Mock()
    process.pid = pid
    process.communicate.side_effect = list(communicate_results)
    return process


def _patches(process: mock.Mock, *, posix: bool) -> tuple:
    """Select the platform branch and fake the launch.

    Returns ``(predicate_patch, popen_patch, popen_mock)`` — the contexts to
    enter plus the launched ``Popen`` mock for call assertions.
    """

    popen_mock = mock.Mock(return_value=process)
    return (
        mock.patch.object(claude, "_posix_process_groups", return_value=posix),
        mock.patch.object(claude.subprocess, "Popen", popen_mock),
        popen_mock,
    )


def _record_spawned_threads() -> tuple:
    """Patch ``threading.Thread`` to capture spawned thread objects.

    Returns ``(patch, spawned)`` — enter the patch, and ``spawned`` then
    holds the real threads the code under test started so the test can
    join them deterministically instead of polling asynchronous closes.
    """
    spawned: list = []
    real_thread = threading.Thread

    def _record(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        spawned.append(thread)
        return thread

    return mock.patch.object(claude.threading, "Thread", _record), spawned


class PosixGroupCleanupTests(unittest.TestCase):
    """Mocked POSIX branch — runs on any host because Popen is faked."""

    def _run(self, process: mock.Mock, killpg_side_effect: "list | None" = None):
        killpg = mock.Mock(side_effect=killpg_side_effect)
        predicate, popen, popen_mock = _patches(process, posix=True)
        with predicate, popen, \
                mock.patch.object(claude.os, "killpg", killpg, create=True), \
                mock.patch.object(claude.signal, "SIGKILL", _SIGKILL, create=True):
            completed = claude._bounded_process(["worker"], timeout=1)
        return completed, killpg, popen_mock

    def test_timeout_terms_the_new_session_group_once(self) -> None:
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1), ("partial", "err"))
        completed, killpg, popen = self._run(process)
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(completed.stdout, "partial")
        self.assertEqual(
            completed.stderr, "err\nworker timed out; process group stopped")
        killpg.assert_called_once_with(4242, _SIGTERM)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_term_grace_expiry_escalates_to_sigkill(self) -> None:
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1),
            subprocess.TimeoutExpired("worker", 5),
            ("partial", "err"))
        completed, killpg, _ = self._run(process)
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(
            [call.args for call in killpg.call_args_list],
            [(4242, _SIGTERM), (4242, _SIGKILL)])

    def test_process_exit_during_kill_escalation_cannot_escape(self) -> None:
        """Regression: the second killpg raced a worker that already exited."""
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1),
            subprocess.TimeoutExpired("worker", 5),
            ("partial", "err"))
        completed, killpg, _ = self._run(process, [None, ProcessLookupError()])
        self.assertEqual(completed.returncode, 124)
        self.assertIn("worker timed out", completed.stderr)
        self.assertEqual(len(killpg.call_args_list), 2)

    def test_process_exit_before_term_is_not_an_error(self) -> None:
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1), ("partial", "err"))
        completed, killpg, _ = self._run(process, [ProcessLookupError()])
        self.assertEqual(completed.returncode, 124)
        killpg.assert_called_once_with(4242, _SIGTERM)

    def test_keyboard_interrupt_stops_the_group_then_propagates(self) -> None:
        process = _fake_process(KeyboardInterrupt(), ("partial", "err"))
        killpg = mock.Mock()
        predicate, popen, _popen_mock = _patches(process, posix=True)
        with predicate, popen, \
                mock.patch.object(claude.os, "killpg", killpg, create=True), \
                mock.patch.object(claude.signal, "SIGKILL", _SIGKILL, create=True):
            with self.assertRaises(KeyboardInterrupt):
                claude._bounded_process(["worker"], timeout=1)
        killpg.assert_called_once_with(4242, _SIGTERM)


class NonPosixStopTests(unittest.TestCase):
    """Mocked non-POSIX branch — Popen terminate/kill, never killpg.

    ``_stop_process_tree`` is pinned ``False`` so these tests exercise the
    direct-child fallback deterministically even on a real Windows host
    (where the unpatched helper would invoke the real ``taskkill``).
    """

    def _run(self, process: mock.Mock):
        killpg = mock.Mock()
        predicate, popen, popen_mock = _patches(process, posix=False)
        with predicate, popen, \
                mock.patch.object(claude, "_stop_process_tree", return_value=False), \
                mock.patch.object(claude.os, "killpg", killpg, create=True):
            completed = claude._bounded_process(["worker"], timeout=1)
        return completed, killpg, popen_mock

    def test_timeout_terminates_the_child_once(self) -> None:
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1), ("partial", "err"))
        completed, killpg, popen = self._run(process)
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(
            completed.stderr, "err\nworker timed out; worker process stopped")
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()
        killpg.assert_not_called()
        self.assertNotIn("start_new_session", popen.call_args.kwargs)

    def test_terminate_grace_expiry_escalates_to_kill(self) -> None:
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1),
            subprocess.TimeoutExpired("worker", 5),
            ("partial", "err"))
        completed, _, _ = self._run(process)
        self.assertEqual(completed.returncode, 124)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()

    def test_already_exited_child_does_not_raise(self) -> None:
        """A closed process handle mid-escalation is the goal, not an error."""
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1), ("partial", "err"))
        process.terminate.side_effect = OSError("handle is invalid")
        completed, _, _ = self._run(process)
        self.assertEqual(completed.returncode, 124)
        self.assertIn("worker timed out", completed.stderr)

    def test_keyboard_interrupt_terminates_then_propagates(self) -> None:
        process = _fake_process(KeyboardInterrupt(), ("partial", "err"))
        killpg = mock.Mock()
        predicate, popen, _popen_mock = _patches(process, posix=False)
        with predicate, popen, \
                mock.patch.object(claude, "_stop_process_tree", return_value=False), \
                mock.patch.object(claude.os, "killpg", killpg, create=True):
            with self.assertRaises(KeyboardInterrupt):
                claude._bounded_process(["worker"], timeout=1)
        process.terminate.assert_called_once_with()
        killpg.assert_not_called()


class WindowsTreeStopTests(unittest.TestCase):
    """Mocked Windows branch — ``taskkill /T /F`` is the OS-native tree stop."""

    def _windows(self, run_result: object = None, taskkill: "str | None" = "C:/Windows/System32/taskkill.exe"):
        """Patch the platform predicates; return the subprocess.run mock."""
        run = mock.Mock(
            return_value=run_result if run_result is not None
            else subprocess.CompletedProcess([], 0))
        return run, (
            mock.patch.object(claude.os, "name", "nt"),
            mock.patch.object(
                claude, "_system_taskkill", return_value=taskkill),
            mock.patch.object(claude.subprocess, "run", run),
        )

    def test_taskkill_tree_stop_uses_the_native_tree_flags(self) -> None:
        process = _fake_process(pid=99)
        run, patches = self._windows()
        with patches[0], patches[1], patches[2]:
            self.assertTrue(claude._stop_process_tree(process))
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "C:/Windows/System32/taskkill.exe")
        self.assertEqual(argv[1:], ["/PID", "99", "/T", "/F"])
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            claude._BOUNDED_TREE_KILL_TIMEOUT_SECONDS)

    def test_taskkill_failure_or_absence_is_not_a_tree_stop(self) -> None:
        process = _fake_process(pid=99)
        run, patches = self._windows(
            run_result=subprocess.CompletedProcess([], 1))
        with patches[0], patches[1], patches[2]:
            self.assertFalse(claude._stop_process_tree(process))
        run, patches = self._windows(taskkill=None)
        with patches[0], patches[1], patches[2]:
            self.assertFalse(claude._stop_process_tree(process))
        run.assert_not_called()

    def test_non_windows_hosts_have_no_tree_stop(self) -> None:
        with mock.patch.object(claude.os, "name", "posix"), \
                mock.patch.object(claude.subprocess, "run") as run:
            self.assertFalse(claude._stop_process_tree(_fake_process()))
        run.assert_not_called()

    def test_timeout_tree_stop_skips_terminate_and_bounds_the_drain(self) -> None:
        """A succeeded tree stop claims the tree — never terminate/kill —
        and the post-kill drain runs under its own finite bound."""
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1), ("partial", "err"))
        predicate, popen, _popen_mock = _patches(process, posix=False)
        with predicate, popen, \
                mock.patch.object(claude, "_stop_process_tree",
                                  return_value=True) as tree:
            completed = claude._bounded_process(["worker"], timeout=1)
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(completed.stdout, "partial")
        self.assertEqual(
            completed.stderr, "err\nworker timed out; process tree stopped")
        tree.assert_called_once_with(process)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(
            process.communicate.call_args_list[-1],
            mock.call(timeout=claude._BOUNDED_DRAIN_SECONDS))

    def test_drain_deadline_abandons_capture_with_a_truthful_outcome(self) -> None:
        """A handle holder outside the stopped tree ends the wait bounded —
        the receipt reports the abandoned drain instead of hanging."""
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1),
            subprocess.TimeoutExpired("worker", claude._BOUNDED_DRAIN_SECONDS))
        predicate, popen, _popen_mock = _patches(process, posix=False)
        thread_patch, spawned = _record_spawned_threads()
        with predicate, popen, thread_patch, \
                mock.patch.object(claude, "_stop_process_tree", return_value=True):
            completed = claude._bounded_process(["worker"], timeout=1)
        self.assertEqual(completed.returncode, 124)
        self.assertIsNone(completed.stdout)
        self.assertIn(
            "process tree stopped; output capture abandoned", completed.stderr)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(
            timeout=claude._BOUNDED_STOP_GRACE_SECONDS)
        # The stream closes are deferred to daemon threads — a reader
        # thread's buffered-reader lock can outlive the drain deadline —
        # so the caller never blocks on them; the test joins the spawns.
        self.assertEqual(len(spawned), 2)
        self.assertTrue(all(thread.daemon for thread in spawned))
        for thread in spawned:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()

    def test_unreaped_child_reports_termination_unconfirmed(self) -> None:
        """Regression: the tree stop ran and the drain expired, but the
        final reap wait timed out — the receipt cannot claim the stopped
        tree, so it reports the termination as unconfirmed."""
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1),
            subprocess.TimeoutExpired("worker", claude._BOUNDED_DRAIN_SECONDS))
        process.wait.side_effect = subprocess.TimeoutExpired(
            "worker", claude._BOUNDED_STOP_GRACE_SECONDS)
        predicate, popen, _popen_mock = _patches(process, posix=False)
        with predicate, popen, \
                mock.patch.object(claude, "_stop_process_tree",
                                  return_value=True):
            completed = claude._bounded_process(["worker"], timeout=1)
        self.assertEqual(completed.returncode, 124)
        self.assertIsNone(completed.stdout)
        self.assertIn("termination unconfirmed", completed.stderr)
        self.assertIn("output capture abandoned", completed.stderr)
        self.assertNotIn("process tree stopped", completed.stderr)
        process.kill.assert_called_once_with()

    def test_failed_terminate_and_kill_report_termination_unconfirmed(
            self) -> None:
        """Regression: terminate and kill both erroring while the final
        wait expires leaves the child possibly alive — the receipt reports
        the stop as unconfirmed, never ``worker process stopped``."""
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1),
            subprocess.TimeoutExpired(
                "worker", claude._BOUNDED_STOP_GRACE_SECONDS),
            subprocess.TimeoutExpired("worker", claude._BOUNDED_DRAIN_SECONDS))
        process.terminate.side_effect = OSError("handle is invalid")
        process.kill.side_effect = OSError("handle is invalid")
        process.wait.side_effect = subprocess.TimeoutExpired(
            "worker", claude._BOUNDED_STOP_GRACE_SECONDS)
        predicate, popen, _popen_mock = _patches(process, posix=False)
        with predicate, popen, \
                mock.patch.object(claude, "_stop_process_tree",
                                  return_value=False):
            completed = claude._bounded_process(["worker"], timeout=1)
        self.assertEqual(completed.returncode, 124)
        self.assertIsNone(completed.stdout)
        self.assertIn("termination unconfirmed", completed.stderr)
        self.assertIn("output capture abandoned", completed.stderr)
        self.assertNotIn("worker process stopped", completed.stderr)
        process.terminate.assert_called_once_with()
        self.assertEqual(process.kill.call_count, 2)  # escalate + reap

    def test_direct_child_fallback_drain_is_bounded_too(self) -> None:
        """No OS tree stop: terminate, kill, a bounded drain — and a receipt
        that reports the abandoned capture without claiming descendants."""
        process = _fake_process(
            subprocess.TimeoutExpired("worker", 1),
            subprocess.TimeoutExpired("worker", claude._BOUNDED_STOP_GRACE_SECONDS),
            subprocess.TimeoutExpired("worker", claude._BOUNDED_DRAIN_SECONDS))
        predicate, popen, _popen_mock = _patches(process, posix=False)
        with predicate, popen, \
                mock.patch.object(claude, "_stop_process_tree", return_value=False):
            completed = claude._bounded_process(["worker"], timeout=1)
        self.assertEqual(completed.returncode, 124)
        self.assertIn(
            "worker process stopped; output capture abandoned",
            completed.stderr)
        process.terminate.assert_called_once_with()
        self.assertEqual(process.kill.call_count, 2)  # escalate + reap


class WindowsTaskkillResolutionTests(unittest.TestCase):
    """``taskkill.exe`` resolves from the OS system directory only.

    A lane worker runs inside its own worktree, so the current directory
    and ``PATH`` are worker-reachable — a planted ``taskkill.exe`` is
    exactly what the historical ``shutil.which`` lookup resolved first on
    Windows. Resolution must answer the OS-reported system directory's
    own binary or fail closed to the direct-child fallback; it never
    searches.
    """

    def _windll(self, fill):
        """Fake ``kernel32.GetSystemDirectoryW``; fill(buffer, size)->int."""
        windll = mock.Mock()
        windll.kernel32.GetSystemDirectoryW.side_effect = fill
        return mock.patch.object(claude.ctypes, "windll", windll,
                                 create=True)

    def test_system_directory_is_the_os_reported_directory(self) -> None:
        def fill(buffer, size):
            buffer.value = "C:/Windows/System32"
            return len(buffer.value)

        with mock.patch.object(claude.os, "name", "nt"), self._windll(fill):
            self.assertEqual(
                claude._windows_system_directory(), "C:/Windows/System32")

    def test_failed_or_truncated_read_fails_closed(self) -> None:
        def deny(buffer, size):
            raise OSError("denied")

        with mock.patch.object(claude.os, "name", "nt"):
            with self._windll(lambda buffer, size: 0):
                self.assertIsNone(claude._windows_system_directory())
            # A return at/over the buffer size is the required size on a
            # truncated read — not a usable directory.
            with self._windll(
                    lambda buffer, size: claude._SYSTEM_DIRECTORY_CHARS):
                self.assertIsNone(claude._windows_system_directory())
            with self._windll(deny):
                self.assertIsNone(claude._windows_system_directory())
        # No usable Win32 binding at all is fail-closed too.
        with mock.patch.object(claude.os, "name", "nt"), \
                mock.patch.object(claude.ctypes, "windll", object(),
                                  create=True):
            self.assertIsNone(claude._windows_system_directory())
        if os.name != "nt":
            # The real host here is not Windows, so the real call declines.
            self.assertIsNone(claude._windows_system_directory())

    def test_malicious_cwd_or_path_candidate_is_never_selected(self) -> None:
        """Regression: a worker-planted ``taskkill.exe`` in the current
        directory or ``PATH`` is not the binary the tree stop launches."""
        with tempfile.TemporaryDirectory() as system, \
                tempfile.TemporaryDirectory() as planted:
            system_taskkill = Path(system) / "taskkill.exe"
            planted_taskkill = Path(planted) / "taskkill.exe"
            system_taskkill.write_bytes(b"system")
            planted_taskkill.write_bytes(b"planted")
            planted_taskkill.chmod(0o755)
            previous_cwd = os.getcwd()
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            try:
                os.chdir(planted)
                with mock.patch.object(claude.os, "name", "nt"), \
                        mock.patch.dict(
                            os.environ,
                            {"PATH": planted + os.pathsep
                             + os.environ.get("PATH", "")}), \
                        mock.patch.object(
                            claude, "_windows_system_directory",
                            return_value=system), \
                        mock.patch.object(claude.subprocess, "run", run):
                    # The environment really is poisoned: the removed
                    # shutil.which lookup would pick the planted binary
                    # (current directory first on Windows, PATH here).
                    # which() may answer a ``./taskkill.exe``-style path,
                    # so both sides are compared absolute.
                    self.assertEqual(
                        os.path.normcase(os.path.abspath(
                            shutil.which("taskkill.exe"))),
                        os.path.normcase(str(planted_taskkill)))
                    self.assertTrue(
                        claude._stop_process_tree(_fake_process(pid=99)))
            finally:
                os.chdir(previous_cwd)
        argv = run.call_args.args[0]
        self.assertEqual(
            os.path.normcase(argv[0]), os.path.normcase(str(system_taskkill)))
        self.assertEqual(argv[1:], ["/PID", "99", "/T", "/F"])

    def test_unresolvable_binary_fails_closed_to_the_fallback(self) -> None:
        """No system directory, or no binary inside it, is not a tree stop —
        the caller's bounded direct-child lifecycle takes over."""
        process = _fake_process(pid=99)
        run = mock.Mock()
        with tempfile.TemporaryDirectory() as empty_system:
            with mock.patch.object(claude.os, "name", "nt"), \
                    mock.patch.object(claude, "_windows_system_directory",
                                      return_value=empty_system), \
                    mock.patch.object(claude.subprocess, "run", run):
                self.assertFalse(claude._stop_process_tree(process))
        run.assert_not_called()
        with mock.patch.object(claude.os, "name", "nt"), \
                mock.patch.object(claude, "_windows_system_directory",
                                  return_value=None), \
                mock.patch.object(claude.subprocess, "run", run):
            self.assertFalse(claude._stop_process_tree(process))
        run.assert_not_called()

    @unittest.skipUnless(
        os.name == "nt",
        "real system-directory resolution requires a Windows host")
    def test_real_resolution_answers_the_system_taskkill(self) -> None:
        """Windows CI coverage: the OS API resolves a real absolute
        ``taskkill.exe`` inside the reported system directory."""
        system = claude._windows_system_directory()
        taskkill = claude._system_taskkill()
        self.assertIsNotNone(system)
        self.assertIsNotNone(taskkill)
        self.assertTrue(os.path.isabs(taskkill))
        self.assertTrue(os.path.isfile(taskkill))
        self.assertEqual(os.path.basename(taskkill).lower(), "taskkill.exe")
        self.assertEqual(
            os.path.normcase(os.path.dirname(taskkill)),
            os.path.normcase(system))


class AbandonedDrainTests(unittest.TestCase):
    """The real reader-held ``BufferedReader`` shape behind abandoned drains.

    ``communicate()``'s daemon reader threads hold each captured
    ``BufferedReader``'s internal lock until end-of-file, so a close run
    inline on the caller thread would wait on the very pipe holder that
    outlived the drain bound — the fallback would hang past the deadline
    it exists to enforce. This fixture builds that exact shape portably:
    an ``os.pipe`` read end wrapped by ``io.open`` in binary mode, a
    daemon reader parked in ``read()``, and a write end a holder keeps
    open until the test releases it.
    """

    # The holder releases only when the test allows it, so a close that
    # blocked on the reader's lock could never return inside this bound.
    RETURN_BOUND = 2.0

    def test_returns_bounded_while_a_reader_thread_owns_the_stream(self):
        read_fd, write_fd = os.pipe()
        stream = io.open(read_fd, "rb")
        release = threading.Event()
        reader = threading.Thread(target=stream.read, daemon=True)
        holder = threading.Thread(
            target=lambda: (release.wait(30), os.close(write_fd)),
            daemon=True)
        reader.start()
        holder.start()
        time.sleep(0.1)  # let the reader park inside read()
        process = mock.Mock()
        process.stdout = stream
        process.stderr = None
        process.wait.return_value = 0
        thread_patch, spawned = _record_spawned_threads()
        try:
            with thread_patch:
                started = time.monotonic()
                confirmed = claude._abandon_drain(process)
                elapsed = time.monotonic() - started
        finally:
            release.set()
        # Well inside any holder lifetime: the caller waits on neither
        # the reader's lock nor the holder's write end.
        self.assertLess(elapsed, self.RETURN_BOUND)
        self.assertIs(confirmed, True)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(
            timeout=claude._BOUNDED_STOP_GRACE_SECONDS)
        self.assertEqual(len(spawned), 1)
        self.assertTrue(spawned[0].daemon)
        # Deferred cleanup is real: once the holder lets the pipe reach
        # EOF, the parked close releases the descriptor.
        spawned[0].join(timeout=5)
        self.assertFalse(spawned[0].is_alive())
        self.assertTrue(stream.closed)
        reader.join(timeout=5)
        self.assertFalse(reader.is_alive())

    def test_grace_expiry_returns_false_without_hanging(self) -> None:
        """The bounded wait is the last termination check: its expiry
        means the child's exit was never confirmed, so the caller gets
        False rather than a stop claim it cannot back."""
        process = mock.Mock()
        process.stdout = mock.Mock()
        process.stderr = None
        process.wait.side_effect = subprocess.TimeoutExpired(
            "worker", claude._BOUNDED_STOP_GRACE_SECONDS)
        thread_patch, spawned = _record_spawned_threads()
        with thread_patch:
            started = time.monotonic()
            confirmed = claude._abandon_drain(process)
            elapsed = time.monotonic() - started
        self.assertIs(confirmed, False)
        self.assertLess(elapsed, self.RETURN_BOUND)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(
            timeout=claude._BOUNDED_STOP_GRACE_SECONDS)
        # An unconfirmed child does not cancel deferred descriptor
        # cleanup — the parked close still runs.
        self.assertEqual(len(spawned), 1)
        spawned[0].join(timeout=5)
        process.stdout.close.assert_called_once_with()

    def test_wait_error_returns_false(self) -> None:
        """A wait that cannot establish the child's state is unconfirmed."""
        process = mock.Mock()
        process.stdout = None
        process.stderr = None
        process.wait.side_effect = OSError("handle is invalid")
        self.assertIs(claude._abandon_drain(process), False)

    def test_failed_kill_is_true_when_the_reap_confirms(self) -> None:
        """``kill()`` erroring is not itself unconfirmed: a wait that
        reaps the child still answers True."""
        process = mock.Mock()
        process.stdout = None
        process.stderr = None
        process.kill.side_effect = OSError("handle is invalid")
        process.wait.return_value = -9
        self.assertIs(claude._abandon_drain(process), True)


class RealLaunchTests(unittest.TestCase):
    """Actual subprocess lifecycle — the fixtures are portable."""

    def test_accepts_subprocess_run_capture_kwargs(self) -> None:
        completed = claude._bounded_process(
            [sys.executable, "-c", "print('captured')"], timeout=5,
            capture_output=True, check=False, text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "captured")
        self.assertEqual(completed.stderr, "")

    def test_timeout_returns_124_with_partial_output(self) -> None:
        completed = claude._bounded_process(
            [sys.executable, "-c",
             "import time; print('partial', flush=True); time.sleep(30)"],
            timeout=0.5, capture_output=True, check=False, text=True,
        )
        self.assertEqual(completed.returncode, 124)
        self.assertIn("partial", completed.stdout)
        notes = (("process group stopped",) if os.name == "posix"
                 else ("process tree stopped", "worker process stopped"))
        self.assertTrue(any(note in completed.stderr for note in notes),
                        completed.stderr)

    def test_timeout_receipt_keeps_bytes_streams(self) -> None:
        """Without ``text`` the receipt's streams stay bytes, marker included."""
        completed = claude._bounded_process(
            [sys.executable, "-c",
             "import sys, time; sys.stdout.buffer.write(b'partial'); "
             "sys.stdout.flush(); time.sleep(30)"],
            timeout=0.5, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 124)
        self.assertIn(b"partial", completed.stdout)
        self.assertIn(b"worker timed out", completed.stderr)

    def test_timeout_without_posix_groups_terminates_a_real_child(self) -> None:
        """The non-POSIX direct-child fallback runs a real child lifecycle.

        ``_stop_process_tree`` is pinned ``False`` so the fallback itself —
        not the Windows tree stop — is what the real child proves here; the
        tree path gets its real coverage from ``DescendantCleanupTests``.
        """
        with mock.patch.object(claude, "_posix_process_groups",
                               return_value=False), \
                mock.patch.object(claude, "_stop_process_tree",
                                  return_value=False):
            completed = claude._bounded_process(
                [sys.executable, "-c",
                 "import time; print('partial', flush=True); time.sleep(30)"],
                timeout=0.5, capture_output=True, check=False, text=True,
            )
        self.assertEqual(completed.returncode, 124)
        self.assertIn("partial", completed.stdout)
        self.assertIn("worker process stopped", completed.stderr)


class DescendantCleanupTests(unittest.TestCase):
    """Real worker whose ordinary descendant holds the output pipes open.

    The child is spawned plainly — no new session, no detached flags — so
    it inherits the worker's captured stdout/stderr: while it lives, the
    pipes never reach EOF. A stop that only kills the direct child leaves
    the drain hanging past the deadline; the bounded lifecycle must return
    inside the child's own delay with the descendant stopped, which the
    delayed sentinel proves. This is the POSIX process-group path and the
    Windows ``taskkill /T`` path run for real — mocked direct-child tests
    cannot prove descendant cleanup.
    """

    CHILD_DELAY = 10.0
    # The bound must sit strictly below CHILD_DELAY: a drain that hangs
    # until the descendant exits returns at CHILD_DELAY, so returning under
    # this mark proves the deadline — not the descendant — ended the wait.
    RETURN_BOUND = 8.0
    _CHILD_CODE = (
        "import sys, time\n"
        "time.sleep(float(sys.argv[1]))\n"
        "open(sys.argv[2], 'w', encoding='utf-8').write('survived')\n"
    )

    @classmethod
    def _parent_code(cls, *, parent_exits: bool) -> str:
        return (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {cls._CHILD_CODE!r},"
            " sys.argv[1], sys.argv[2]])\n"
            "print('parent-ready', flush=True)\n"
            + ("sys.exit(0)\n" if parent_exits else "time.sleep(60)\n")
        )

    @unittest.skipUnless(
        os.name in ("posix", "nt"),
        "descendant cleanup is only claimed where the platform offers one "
        "(process group or OS-native tree stop)")
    def test_descendant_holding_pipes_cannot_outrun_the_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "descendant-survived"
            started = time.monotonic()
            completed = claude._bounded_process(
                [sys.executable, "-c",
                 self._parent_code(parent_exits=False),
                 str(self.CHILD_DELAY), str(sentinel)],
                timeout=0.5, capture_output=True, check=False, text=True,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(completed.returncode, 124)
            self.assertLess(elapsed, self.RETURN_BOUND)
            self.assertIn("parent-ready", completed.stdout)
            expected = ("process group stopped" if os.name == "posix"
                        else "process tree stopped")
            self.assertIn(expected, completed.stderr)
            self.assertNotIn("abandoned", completed.stderr)
            # Wait past the child's own delay: supported cleanup means the
            # sentinel never appears.
            while time.monotonic() - started < self.CHILD_DELAY + 2:
                time.sleep(0.1)
            self.assertFalse(
                sentinel.exists(),
                "descendant holding the output pipes outlived the timeout")

    @unittest.skipUnless(
        os.name in ("posix", "nt"),
        "the control only needs a platform where the fixture runs")
    def test_descendant_keeps_pipes_open_past_the_parent_exit(self) -> None:
        """Control for the fixture: the child really inherits the handles —
        the drain waits past the parent's own exit for the descendant's
        EOF, and the uncancelled child writes its sentinel."""
        delay = 3.0
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "descendant-ran"
            started = time.monotonic()
            completed = claude._bounded_process(
                [sys.executable, "-c",
                 self._parent_code(parent_exits=True),
                 str(delay), str(sentinel)],
                timeout=30, capture_output=True, check=False, text=True,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(completed.returncode, 0)
            self.assertIn("parent-ready", completed.stdout)
            # The parent's exit did not close the pipe: communicate only
            # returned when the descendant's exit released the handles.
            self.assertGreater(elapsed, delay * 0.6)
            self.assertTrue(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
