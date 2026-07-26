import os
import signal
import subprocess
import sys
import textwrap
import threading
from threading import Event

import pytest

import video_auto_editor.runtime.cancellation as cancellation_module
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
    SignalCoordinator,
    SignalDisposition,
)
from video_auto_editor.runtime.errors import ExitCode


def test_root_cancellation_is_visible_to_every_consumer_token():
    source = CancellationSource()
    first_consumer = source.token
    second_consumer = source.token

    assert source.request(signal.SIGTERM) is True

    assert first_consumer is second_consumer
    assert first_consumer.cancelled is True
    assert first_consumer.signal_number == signal.SIGTERM
    assert first_consumer.exit_code is ExitCode.SIGTERM
    assert first_consumer.wait(timeout=0) is True
    with pytest.raises(CancellationRequested) as raised:
        second_consumer.raise_if_cancelled()
    assert raised.value.signal_number == signal.SIGTERM


def test_first_signal_starts_one_ten_second_cleanup_window():
    source = CancellationSource(clock=lambda: 42.0)

    assert source.request(signal.SIGINT) is True
    assert source.request(signal.SIGTERM) is False

    assert source.token.signal_number == signal.SIGINT
    assert source.token.requested_at == 42.0
    assert source.token.cleanup_deadline == 52.0


def test_signal_coordinator_starts_a_watchdog_for_controlled_cleanup():
    timers = []
    cleanup_calls = []
    exit_calls = []

    class FakeTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

    def timer_factory(delay, callback):
        timer = FakeTimer(delay, callback)
        timers.append(timer)
        return timer

    source = CancellationSource(clock=lambda: 5.0)
    coordinator = SignalCoordinator(
        source,
        best_effort_cleanup=lambda: cleanup_calls.append("cleanup"),
        hard_exit=exit_calls.append,
        timer_factory=timer_factory,
    )

    disposition = coordinator.handle_signal(signal.SIGINT)

    assert disposition is SignalDisposition.CONTROLLED_CLEANUP_STARTED
    assert source.token.cancelled is True
    assert len(timers) == 1
    assert timers[0].delay == 10.0
    assert timers[0].started is True
    assert cleanup_calls == []
    assert exit_calls == []

    coordinator.complete_cleanup()
    assert timers[0].cancelled is True

    timers[0].callback()
    assert cleanup_calls == []
    assert exit_calls == []


def test_second_signal_forces_best_effort_cleanup_and_immediate_exit():
    events = []
    cleanup_started = Event()
    release_cleanup = Event()
    exit_calls = []

    class PassiveTimer:
        def start(self):
            events.append("watchdog_started")

        def cancel(self):
            events.append("watchdog_cancelled")

    def blocking_cleanup():
        cleanup_started.set()
        release_cleanup.wait(timeout=2)

    coordinator = SignalCoordinator(
        CancellationSource(clock=lambda: 8.0),
        best_effort_cleanup=blocking_cleanup,
        hard_exit=exit_calls.append,
        timer_factory=lambda _delay, _callback: PassiveTimer(),
    )

    coordinator.handle_signal(signal.SIGTERM)
    coordinator.complete_cleanup()
    disposition = coordinator.handle_signal(signal.SIGINT)

    assert disposition is SignalDisposition.FORCED_EXIT_STARTED
    assert exit_calls == [ExitCode.SIGTERM]
    assert cleanup_started.wait(timeout=1)
    release_cleanup.set()
    assert events[-1] == "watchdog_cancelled"


def test_watchdog_forces_exit_when_controlled_cleanup_reaches_its_deadline():
    callbacks = []
    cleanup_started = Event()
    exit_calls = []

    class CapturedTimer:
        def __init__(self, callback):
            self.callback = callback

        def start(self):
            callbacks.append(self.callback)

        def cancel(self):
            pass

    coordinator = SignalCoordinator(
        CancellationSource(clock=lambda: 8.0),
        best_effort_cleanup=cleanup_started.set,
        hard_exit=exit_calls.append,
        timer_factory=lambda _delay, callback: CapturedTimer(callback),
    )

    coordinator.handle_signal(signal.SIGINT)
    callbacks[0]()

    assert exit_calls == [ExitCode.SIGINT]
    assert cleanup_started.wait(timeout=1)


def test_watchdog_delay_is_recomputed_from_the_absolute_cleanup_deadline():
    current_time = [5.0]
    timers = []

    class CapturedTimer:
        def __init__(self, delay):
            self.delay = delay
            self.cancelled = False

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    def slow_timer_factory(delay, _callback):
        timer = CapturedTimer(delay)
        timers.append(timer)
        if len(timers) == 1:
            current_time[0] += 4.0
        return timer

    coordinator = SignalCoordinator(
        CancellationSource(clock=lambda: current_time[0]),
        best_effort_cleanup=lambda: None,
        hard_exit=lambda _code: None,
        timer_factory=slow_timer_factory,
    )

    coordinator.handle_signal(signal.SIGINT)

    assert [timer.delay for timer in timers] == [10.0, 6.0]
    assert timers[0].cancelled is True


def test_watchdog_delay_is_recomputed_when_timer_start_consumes_time():
    current_time = [5.0]
    timers = []

    class SlowStartingTimer:
        def __init__(self, delay):
            self.delay = delay
            self.cancelled = False

        def start(self):
            if len(timers) == 1:
                current_time[0] += 3.0

        def cancel(self):
            self.cancelled = True

    def timer_factory(delay, _callback):
        timer = SlowStartingTimer(delay)
        timers.append(timer)
        return timer

    coordinator = SignalCoordinator(
        CancellationSource(clock=lambda: current_time[0]),
        best_effort_cleanup=lambda: None,
        hard_exit=lambda _code: None,
        timer_factory=timer_factory,
    )

    coordinator.handle_signal(signal.SIGINT)

    assert [timer.delay for timer in timers] == [10.0, 7.0]
    assert timers[0].cancelled is True


@pytest.mark.parametrize("failure_phase", ["factory", "start"])
def test_watchdog_setup_failure_cannot_leave_an_unbounded_cleanup_window(
    failure_phase,
):
    cleanup_started = Event()
    exit_calls = []

    class BrokenTimer:
        def start(self):
            raise RuntimeError("timer start failed")

        def cancel(self):
            pass

    def timer_factory(_delay, _callback):
        if failure_phase == "factory":
            raise RuntimeError("timer factory failed")
        return BrokenTimer()

    coordinator = SignalCoordinator(
        CancellationSource(),
        best_effort_cleanup=cleanup_started.set,
        hard_exit=exit_calls.append,
        timer_factory=timer_factory,
    )

    disposition = coordinator.handle_signal(signal.SIGTERM)

    assert disposition is SignalDisposition.FORCED_EXIT_STARTED
    assert exit_calls == [ExitCode.SIGTERM]
    assert cleanup_started.wait(timeout=1)


def test_cancellation_clock_failure_cannot_leave_an_unbounded_cleanup_window():
    def broken_clock():
        raise RuntimeError("clock failed")

    cleanup_started = Event()
    exit_calls = []
    coordinator = SignalCoordinator(
        CancellationSource(clock=broken_clock),
        best_effort_cleanup=cleanup_started.set,
        hard_exit=exit_calls.append,
    )

    disposition = coordinator.handle_signal(signal.SIGINT)

    assert disposition is SignalDisposition.FORCED_EXIT_STARTED
    assert exit_calls == [ExitCode.SIGINT]
    assert cleanup_started.wait(timeout=1)


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="真实信号重入测试只适用于 POSIX",
)
def test_real_first_signal_is_visible_before_the_python_handler_returns():
    script = textwrap.dedent(
        """
        import os
        import signal

        from video_auto_editor.runtime.cancellation import (
            CancellationSource,
            SignalCoordinator,
        )

        source = CancellationSource()
        coordinator = SignalCoordinator(
            source,
            best_effort_cleanup=lambda: None,
        )
        coordinator.install()
        os.kill(os.getpid(), signal.SIGINT)
        os._exit(0 if source.token.cancelled else 99)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        check=False,
        timeout=3,
    )

    assert completed.returncode == 0


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="真实信号重入测试只适用于 POSIX",
)
def test_real_second_signal_forces_exit_without_waiting_for_the_dispatcher():
    script = textwrap.dedent(
        """
        import os
        import signal
        import sys

        from video_auto_editor.runtime.cancellation import (
            CancellationSource,
            SignalCoordinator,
        )

        source = CancellationSource()
        coordinator = SignalCoordinator(
            source,
            best_effort_cleanup=lambda: os.write(
                sys.stdout.fileno(),
                b"cancelled" if source.token.cancelled else b"not-cancelled",
            ),
        )
        coordinator.install()
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGTERM)
        os._exit(99)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        check=False,
        capture_output=True,
        timeout=3,
    )

    assert completed.returncode == ExitCode.SIGINT
    assert completed.stdout == b"cancelled"


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="真实信号重入测试只适用于 POSIX",
)
def test_reentrant_second_signal_cannot_deadlock_the_signal_handler():
    script = textwrap.dedent(
        """
        import os
        import signal
        import time

        from video_auto_editor.runtime.cancellation import (
            CancellationSource,
            SignalCoordinator,
        )

        class PassiveTimer:
            def start(self):
                pass

            def cancel(self):
                pass

        def reentrant_timer_factory(_delay, _callback):
            os.kill(os.getpid(), signal.SIGTERM)
            return PassiveTimer()

        coordinator = SignalCoordinator(
            CancellationSource(),
            best_effort_cleanup=lambda: None,
            timer_factory=reentrant_timer_factory,
        )
        coordinator.install()
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(1.0)
        os._exit(99)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        check=False,
        timeout=3,
    )

    assert completed.returncode == ExitCode.SIGINT


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="真实信号清理测试只适用于 POSIX",
)
def test_real_second_signal_gives_best_effort_cleanup_a_bounded_execution_chance():
    script = textwrap.dedent(
        """
        import os
        import signal
        import sys
        import time

        from video_auto_editor.runtime.cancellation import (
            CancellationSource,
            SignalCoordinator,
        )

        source = CancellationSource()
        coordinator = SignalCoordinator(
            source,
            best_effort_cleanup=lambda: os.write(
                sys.stdout.fileno(),
                b"cleanup-ran",
            ),
        )
        coordinator.install()
        os.kill(os.getpid(), signal.SIGINT)
        deadline = time.monotonic() + 1.0
        while not source.token.cancelled and time.monotonic() < deadline:
            time.sleep(0.001)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1.0)
        os._exit(99)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        check=False,
        capture_output=True,
        timeout=3,
    )

    assert completed.returncode == ExitCode.SIGINT
    assert completed.stdout == b"cleanup-ran"


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="真实信号清理测试只适用于 POSIX",
)
def test_real_second_signal_does_not_wait_for_blocked_best_effort_cleanup():
    script = textwrap.dedent(
        """
        import os
        import signal
        import time

        from video_auto_editor.runtime.cancellation import (
            CancellationSource,
            SignalCoordinator,
        )

        coordinator = SignalCoordinator(
            CancellationSource(),
            best_effort_cleanup=lambda: time.sleep(5.0),
        )
        coordinator.install()
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGTERM)
        os._exit(99)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        check=False,
        timeout=1,
    )

    assert completed.returncode == ExitCode.SIGINT


def test_signal_coordinator_installs_and_restores_both_signal_handlers():
    handlers = {
        signal.SIGINT: "previous-int",
        signal.SIGTERM: "previous-term",
    }

    def set_handler(signal_number, handler):
        previous = handlers[signal_number]
        handlers[signal_number] = handler
        return previous

    wakeup_fd = {"current": -1}

    def set_wakeup_fd(file_descriptor):
        previous = wakeup_fd["current"]
        wakeup_fd["current"] = file_descriptor
        return previous

    coordinator = SignalCoordinator(
        CancellationSource(),
        best_effort_cleanup=lambda: None,
        hard_exit=lambda _code: None,
        timer_factory=lambda _delay, _callback: None,
        signal_setter=set_handler,
        wakeup_fd_setter=set_wakeup_fd,
    )

    coordinator.install()
    assert callable(handlers[signal.SIGINT])
    assert callable(handlers[signal.SIGTERM])
    assert handlers[signal.SIGINT] != coordinator.handle_signal
    assert handlers[signal.SIGTERM] != coordinator.handle_signal
    assert wakeup_fd["current"] >= 0

    coordinator.restore()
    assert handlers == {
        signal.SIGINT: "previous-int",
        signal.SIGTERM: "previous-term",
    }
    assert wakeup_fd["current"] == -1

    coordinator.install()
    coordinator.restore()
    coordinator.handle_signal(signal.SIGTERM)

    with pytest.raises(RuntimeError, match="处理信号并恢复后不能再次安装"):
        coordinator.install()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="文件描述符回收检查需要 Linux procfs",
)
def test_dispatcher_start_failure_preserves_the_error_and_rolls_back_resources(
    monkeypatch,
):
    expected_error = OSError("dispatcher start failed")
    original_thread = threading.Thread
    baseline_fds = frozenset(os.listdir("/proc/self/fd"))
    baseline_threads = frozenset(threading.enumerate())

    def fail_dispatcher_start():
        raise expected_error

    def thread_factory(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        if kwargs.get("name") == "video-auto-editor-signal-dispatcher":
            thread.start = fail_dispatcher_start
        return thread

    monkeypatch.setattr(cancellation_module, "Thread", thread_factory)
    coordinator = SignalCoordinator(
        CancellationSource(),
        best_effort_cleanup=lambda: None,
    )

    with pytest.raises(OSError) as raised:
        coordinator.install()

    assert raised.value is expected_error
    assert frozenset(os.listdir("/proc/self/fd")) == baseline_fds
    assert frozenset(threading.enumerate()) == baseline_threads


def test_restore_cannot_discard_an_incomplete_controlled_cleanup():
    timer_started = Event()
    timers = []
    source = CancellationSource()

    class CapturedTimer:
        def __init__(self):
            self.cancelled = False

        def start(self):
            timer_started.set()

        def cancel(self):
            self.cancelled = True

    coordinator = SignalCoordinator(
        source,
        best_effort_cleanup=lambda: None,
        hard_exit=lambda _code: None,
        timer_factory=lambda _delay, _callback: (
            timers.append(CapturedTimer()) or timers[-1]
        ),
    )
    coordinator.install()
    try:
        os.kill(os.getpid(), signal.SIGINT)
        assert source.token.cancelled is True

        with pytest.raises(RuntimeError, match="受控清理尚未完成"):
            coordinator.restore()

        assert timer_started.wait(timeout=1)
        assert timers[0].cancelled is False
    finally:
        coordinator.complete_cleanup()
        coordinator.restore()

    assert timers[0].cancelled is True


def test_signal_coordinator_rejects_an_already_cancelled_source():
    source = CancellationSource()
    source.request(signal.SIGINT)

    with pytest.raises(ValueError, match="必须绑定尚未取消"):
        SignalCoordinator(
            source,
            best_effort_cleanup=lambda: None,
        )


def test_source_cancelled_after_coordinator_creation_is_treated_as_a_repeat():
    timers = []
    exit_calls = []
    source = CancellationSource()
    coordinator = SignalCoordinator(
        source,
        best_effort_cleanup=lambda: None,
        hard_exit=exit_calls.append,
        timer_factory=lambda delay, callback: timers.append((delay, callback)),
    )
    source.request(signal.SIGTERM)

    disposition = coordinator.handle_signal(signal.SIGINT)

    assert disposition is SignalDisposition.FORCED_EXIT_STARTED
    assert timers == []
    assert exit_calls == [ExitCode.SIGTERM]
