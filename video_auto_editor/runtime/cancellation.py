"""线程安全的根取消与信号传播机制。"""

from dataclasses import dataclass
from enum import Enum
import math
import os
from queue import SimpleQueue
import signal
from threading import (
    Condition,
    Event,
    RLock,
    Thread,
    Timer,
    current_thread,
    main_thread,
)
from time import monotonic
from typing import Any, Callable, Protocol

from .errors import ExitCode


MAX_CLEANUP_SECONDS = 10.0
FORCED_CLEANUP_GRACE_SECONDS = 0.05
_WATCHDOG_RECALC_THRESHOLD_SECONDS = 0.001
_HANDLED_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM})


def _signal_exit_code(signal_number: int) -> ExitCode:
    if signal_number == signal.SIGINT:
        return ExitCode.SIGINT
    if signal_number == signal.SIGTERM:
        return ExitCode.SIGTERM
    raise ValueError("只支持 SIGINT 或 SIGTERM")


def _read_clock(clock: Callable[[], float]) -> float:
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("取消单调时钟必须返回有限数值")
    return float(value)


class _CancellationState:
    __slots__ = (
        "cleanup_timeout_seconds",
        "clock",
        "condition",
        "snapshot",
    )

    def __init__(
        self,
        clock: Callable[[], float],
        cleanup_timeout_seconds: float,
    ) -> None:
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.clock = clock
        self.condition = Condition(RLock())
        self.snapshot: _CancellationSnapshot | None = None


@dataclass(frozen=True, slots=True)
class _CancellationSnapshot:
    signal_number: int
    requested_at: float
    cleanup_deadline: float


class CancellationRequested(Exception):
    """深模块在协作取消检查点观察到根取消。"""

    def __init__(self, signal_number: int | None) -> None:
        self.signal_number = signal_number
        super().__init__("直播拆条运行已请求取消")


@dataclass(frozen=True, slots=True)
class CancellationToken:
    """深模块只能观察、不能发起的根取消令牌。"""

    _state: _CancellationState

    @property
    def cancelled(self) -> bool:
        return self._state.snapshot is not None

    @property
    def signal_number(self) -> int | None:
        snapshot = self._state.snapshot
        return snapshot.signal_number if snapshot is not None else None

    @property
    def requested_at(self) -> float | None:
        snapshot = self._state.snapshot
        return snapshot.requested_at if snapshot is not None else None

    @property
    def cleanup_deadline(self) -> float | None:
        snapshot = self._state.snapshot
        return snapshot.cleanup_deadline if snapshot is not None else None

    @property
    def exit_code(self) -> ExitCode | None:
        signal_number = self.signal_number
        return _signal_exit_code(signal_number) if signal_number is not None else None

    def wait(self, timeout: float | None = None) -> bool:
        """等待根取消，返回是否已经取消。"""
        with self._state.condition:
            return self._state.condition.wait_for(
                lambda: self._state.snapshot is not None,
                timeout,
            )

    def raise_if_cancelled(self) -> None:
        """在深模块协作检查点停止继续业务工作。"""
        if self.cancelled:
            raise CancellationRequested(self.signal_number)

    def raise_if_cancelled_or_signal_pending(self) -> None:
        """在屏蔽信号的提交边界观察已经到达的根中断。"""
        self.raise_if_cancelled()
        pending_signals = signal.sigpending()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            if signal_number in pending_signals:
                raise CancellationRequested(signal_number)


class CancellationSource:
    """由应用独占、向所有深模块传播的根取消源。"""

    __slots__ = ("_state", "_token")

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        cleanup_timeout_seconds: float = MAX_CLEANUP_SECONDS,
    ) -> None:
        if (
            isinstance(cleanup_timeout_seconds, bool)
            or not isinstance(cleanup_timeout_seconds, (int, float))
            or not math.isfinite(cleanup_timeout_seconds)
            or not 0 < cleanup_timeout_seconds <= MAX_CLEANUP_SECONDS
        ):
            raise ValueError("受控清理窗口必须大于零且不超过十秒")
        self._state = _CancellationState(clock, float(cleanup_timeout_seconds))
        self._token = CancellationToken(self._state)

    @property
    def token(self) -> CancellationToken:
        return self._token

    def request(self, signal_number: int) -> bool:
        """接受首次信号；重复请求保持首次信号事实不变。"""
        _signal_exit_code(signal_number)
        with self._state.condition:
            if self._state.snapshot is not None:
                return False
            requested_at = _read_clock(self._state.clock)
            if self._state.snapshot is not None:
                return False
            self._state.snapshot = _CancellationSnapshot(
                signal_number=signal_number,
                requested_at=requested_at,
                cleanup_deadline=(
                    requested_at + self._state.cleanup_timeout_seconds
                ),
            )
            self._state.condition.notify_all()
            return True

    def _request_from_signal(self, signal_number: int) -> bool:
        """在 Python signal handler 中以一次引用发布取消快照。"""
        _signal_exit_code(signal_number)
        if self._state.snapshot is not None:
            return False
        requested_at = _read_clock(self._state.clock)
        snapshot = _CancellationSnapshot(
            signal_number=signal_number,
            requested_at=requested_at,
            cleanup_deadline=requested_at + self._state.cleanup_timeout_seconds,
        )
        if self._state.snapshot is not None:
            return False
        self._state.snapshot = snapshot
        return True

    def _notify_waiters(self) -> None:
        with self._state.condition:
            self._state.condition.notify_all()

    def _remaining_cleanup_seconds(self) -> float:
        snapshot = self._state.snapshot
        if snapshot is None:
            raise RuntimeError("根取消尚未建立受控清理窗口")
        current = _read_clock(self._state.clock)
        if current < snapshot.requested_at:
            raise ValueError("取消单调时钟不能倒退")
        return max(0.0, snapshot.cleanup_deadline - current)


class SignalDisposition(str, Enum):
    """信号协调器完成的稳定动作。"""

    CONTROLLED_CLEANUP_STARTED = "controlled_cleanup_started"
    FORCED_EXIT_STARTED = "forced_exit_started"


class _TimerHandle(Protocol):
    def start(self) -> None:
        ...

    def cancel(self) -> None:
        ...


_TimerFactory = Callable[[float, Callable[[], None]], _TimerHandle]
_SignalSetter = Callable[[int, Any], Any]
_WakeupFdSetter = Callable[[int], int]


def _watchdog_timer(
    delay: float,
    callback: Callable[[], None],
) -> _TimerHandle:
    timer = Timer(delay, callback)
    timer.daemon = True
    return timer


class SignalCoordinator:
    """以 wakeup fd 把真实信号移出主线程处理器。"""

    __slots__ = (
        "_best_effort_cleanup",
        "_cleanup_dispatched",
        "_cleanup_finished",
        "_cleanup_requests",
        "_cleanup_thread",
        "_completed",
        "_dispatcher_stop",
        "_dispatcher_thread",
        "_first_signal",
        "_force_requested",
        "_hard_exit",
        "_hard_exit_started",
        "_installing",
        "_lock",
        "_pipe_read_fd",
        "_pipe_write_fd",
        "_previous_handlers",
        "_previous_wakeup_fd",
        "_setup_requests",
        "_setup_thread",
        "_signal_setter",
        "_source",
        "_timer",
        "_timer_factory",
        "_wakeup_fd_setter",
        "_workers_running",
    )

    def __init__(
        self,
        source: CancellationSource,
        *,
        best_effort_cleanup: Callable[[], None],
        hard_exit: Callable[[int], None] = os._exit,
        timer_factory: _TimerFactory = _watchdog_timer,
        signal_setter: _SignalSetter = signal.signal,
        wakeup_fd_setter: _WakeupFdSetter = signal.set_wakeup_fd,
    ) -> None:
        if not isinstance(source, CancellationSource):
            raise TypeError("信号协调器必须绑定 CancellationSource")
        if source.token.cancelled:
            raise ValueError("信号协调器必须绑定尚未取消的根取消源")
        self._best_effort_cleanup = best_effort_cleanup
        self._cleanup_dispatched = False
        self._cleanup_finished = Event()
        self._cleanup_requests: SimpleQueue[bool] | None = None
        self._cleanup_thread: Thread | None = None
        self._completed = False
        self._dispatcher_stop = Event()
        self._dispatcher_thread: Thread | None = None
        self._first_signal: int | None = None
        self._force_requested = False
        self._hard_exit = hard_exit
        self._hard_exit_started = False
        self._installing = False
        self._lock = RLock()
        self._pipe_read_fd: int | None = None
        self._pipe_write_fd: int | None = None
        self._previous_handlers: dict[int, Any] | None = None
        self._previous_wakeup_fd: int | None = None
        self._setup_requests: SimpleQueue[int | None] | None = None
        self._setup_thread: Thread | None = None
        self._signal_setter = signal_setter
        self._source = source
        self._timer: _TimerHandle | None = None
        self._timer_factory = timer_factory
        self._wakeup_fd_setter = wakeup_fd_setter
        self._workers_running = False

    def install(self) -> None:
        """在主线程安装轻量处理器和保持到达顺序的 wakeup fd。"""
        if current_thread() is not main_thread():
            raise RuntimeError("信号协调器只能在主线程安装")
        with self._lock:
            if self._previous_handlers is not None:
                raise RuntimeError("信号协调器已经安装")
            if self._installing:
                raise RuntimeError("信号协调器正在安装")
            if self._first_signal is not None:
                raise RuntimeError("信号协调器处理信号并恢复后不能再次安装")
            self._installing = True

        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _HANDLED_SIGNALS)
        previous_handlers: dict[int, Any] = {}
        previous_wakeup_fd: int | None = None
        read_fd: int | None = None
        write_fd: int | None = None
        try:
            read_fd, write_fd = os.pipe()
            os.set_blocking(write_fd, False)
            self._ensure_workers()
            self._start_dispatcher(read_fd)
            previous_wakeup_fd = self._wakeup_fd_setter(write_fd)
            for handled_signal in _HANDLED_SIGNALS:
                previous_handlers[handled_signal] = self._signal_setter(
                    handled_signal,
                    self._capture_signal,
                )
            with self._lock:
                self._pipe_read_fd = read_fd
                self._pipe_write_fd = write_fd
                self._previous_handlers = previous_handlers
                self._previous_wakeup_fd = previous_wakeup_fd
        except BaseException:
            for installed_signal, previous in reversed(previous_handlers.items()):
                try:
                    self._signal_setter(installed_signal, previous)
                except BaseException:
                    pass
            if previous_wakeup_fd is not None:
                try:
                    self._wakeup_fd_setter(previous_wakeup_fd)
                except BaseException:
                    pass
            self._stop_dispatcher(read_fd, write_fd)
            self._stop_workers()
            raise
        finally:
            with self._lock:
                self._installing = False
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def restore(self) -> None:
        """在主线程恢复安装前的处理器和 wakeup fd。"""
        if current_thread() is not main_thread():
            raise RuntimeError("信号协调器只能在主线程恢复")
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _HANDLED_SIGNALS)
        restored_signals: list[int] = []
        try:
            with self._lock:
                if self._installing:
                    raise RuntimeError("信号协调器正在安装，不能恢复")
                if self._first_signal is not None and not self._completed:
                    raise RuntimeError(
                        "受控清理尚未完成，不能恢复信号处理器"
                    )
                previous_handlers = self._previous_handlers
                previous_wakeup_fd = self._previous_wakeup_fd
                read_fd = self._pipe_read_fd
                write_fd = self._pipe_write_fd
            if previous_handlers is None:
                self._stop_workers()
                return
            if previous_wakeup_fd is None:
                raise RuntimeError("已安装协调器必须保存原 wakeup fd")
            for signal_number, previous in previous_handlers.items():
                self._signal_setter(signal_number, previous)
                restored_signals.append(signal_number)
            self._wakeup_fd_setter(previous_wakeup_fd)
            with self._lock:
                self._previous_handlers = None
                self._previous_wakeup_fd = None
                self._pipe_read_fd = None
                self._pipe_write_fd = None
                timer = self._timer
        except BaseException:
            for signal_number in restored_signals:
                try:
                    self._signal_setter(signal_number, self._capture_signal)
                except BaseException:
                    pass
            raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

        _cancel_timer(timer)
        self._stop_dispatcher(read_fd, write_fd)
        self._stop_workers()

    def handle_signal(
        self,
        signal_number: int,
        _frame: object | None = None,
    ) -> SignalDisposition:
        """供确定性组合与测试直接接受信号；真实安装路径使用 wakeup fd。"""
        _signal_exit_code(signal_number)
        self._ensure_workers()
        first_signal, timer = self._claim_signal(signal_number)
        if not first_signal:
            self._begin_forced_exit(
                force_after_completion=True,
                repeated_signal=True,
            )
            _cancel_timer(timer)
            return SignalDisposition.FORCED_EXIT_STARTED
        return self._start_controlled_cleanup(signal_number)

    def complete_cleanup(self) -> None:
        """在受控清理完成后撤销强退看门狗。"""
        with self._lock:
            self._completed = True
            timer = self._timer
        _cancel_timer(timer)

    def _capture_signal(
        self,
        signal_number: int,
        _frame: object | None = None,
    ) -> None:
        """真实 Python 处理器只发布不可变状态并投递预启动 worker。"""
        _signal_exit_code(signal_number)
        if self._first_signal is not None:
            self._begin_forced_exit_from_signal(repeated_signal=True)
            return
        self._first_signal = signal_number
        try:
            accepted = self._source._request_from_signal(signal_number)
        except BaseException:
            self._begin_forced_exit_from_signal(repeated_signal=False)
            return
        if not accepted:
            existing_signal = self._source.token.signal_number
            self._first_signal = existing_signal or signal_number
            self._begin_forced_exit_from_signal(repeated_signal=True)
            return
        requests = self._setup_requests
        if requests is None:
            self._begin_forced_exit_from_signal(repeated_signal=False)
            return
        requests.put(signal_number)

    def _claim_signal(
        self,
        signal_number: int,
    ) -> tuple[bool, _TimerHandle | None]:
        with self._lock:
            first_signal = self._first_signal is None
            if first_signal:
                self._first_signal = signal_number
            return first_signal, self._timer

    def _dispatch_signal(self, signal_number: int) -> None:
        # wakeup fd 只负责唤醒并排空内核信号字节；业务状态由 Python handler
        # 在返回前发布，避免 dispatcher 调度延迟改变提交点语义。
        _signal_exit_code(signal_number)

    def _start_controlled_cleanup(
        self,
        signal_number: int,
    ) -> SignalDisposition:
        try:
            accepted = self._source.request(signal_number)
        except BaseException:
            self._begin_forced_exit(force_after_completion=False)
            return SignalDisposition.FORCED_EXIT_STARTED
        if not accepted:
            existing_signal = self._source.token.signal_number
            with self._lock:
                self._first_signal = existing_signal or signal_number
            self._begin_forced_exit(
                force_after_completion=True,
                repeated_signal=True,
            )
            return SignalDisposition.FORCED_EXIT_STARTED
        return self._start_watchdog()

    def _start_watchdog(self) -> SignalDisposition:
        while True:
            try:
                delay = self._source._remaining_cleanup_seconds()
            except BaseException:
                self._begin_forced_exit(force_after_completion=False)
                return SignalDisposition.FORCED_EXIT_STARTED
            if delay <= 0:
                self._begin_forced_exit(force_after_completion=False)
                return SignalDisposition.FORCED_EXIT_STARTED
            try:
                timer = self._timer_factory(delay, self._watchdog_expired)
            except BaseException:
                self._begin_forced_exit(force_after_completion=False)
                return SignalDisposition.FORCED_EXIT_STARTED
            try:
                remaining_after_factory = (
                    self._source._remaining_cleanup_seconds()
                )
            except BaseException:
                _cancel_timer(timer)
                self._begin_forced_exit(force_after_completion=False)
                return SignalDisposition.FORCED_EXIT_STARTED
            if remaining_after_factory <= 0:
                _cancel_timer(timer)
                self._begin_forced_exit(force_after_completion=False)
                return SignalDisposition.FORCED_EXIT_STARTED
            if (
                delay - remaining_after_factory
                > _WATCHDOG_RECALC_THRESHOLD_SECONDS
            ):
                _cancel_timer(timer)
                continue

            with self._lock:
                self._timer = timer
                should_start = (
                    not self._force_requested and not self._completed
                )
                force_requested = self._force_requested
            if not should_start:
                _cancel_timer(timer)
                if force_requested:
                    return SignalDisposition.FORCED_EXIT_STARTED
                return SignalDisposition.CONTROLLED_CLEANUP_STARTED
            try:
                timer.start()
            except BaseException:
                _cancel_timer(timer)
                self._begin_forced_exit(force_after_completion=False)
                return SignalDisposition.FORCED_EXIT_STARTED
            try:
                remaining_after_start = (
                    self._source._remaining_cleanup_seconds()
                )
            except BaseException:
                _cancel_timer(timer)
                self._begin_forced_exit(force_after_completion=False)
                return SignalDisposition.FORCED_EXIT_STARTED
            with self._lock:
                force_requested = self._force_requested
                completed = self._completed
            if force_requested or completed:
                _cancel_timer(timer)
                if force_requested:
                    return SignalDisposition.FORCED_EXIT_STARTED
                return SignalDisposition.CONTROLLED_CLEANUP_STARTED
            if remaining_after_start <= 0:
                _cancel_timer(timer)
                self._begin_forced_exit(force_after_completion=False)
                return SignalDisposition.FORCED_EXIT_STARTED
            if (
                remaining_after_factory - remaining_after_start
                > _WATCHDOG_RECALC_THRESHOLD_SECONDS
            ):
                _cancel_timer(timer)
                with self._lock:
                    if self._timer is timer:
                        self._timer = None
                continue
            return SignalDisposition.CONTROLLED_CLEANUP_STARTED

    def _watchdog_expired(self) -> None:
        self._begin_forced_exit(force_after_completion=False)

    def _begin_forced_exit(
        self,
        *,
        force_after_completion: bool,
        repeated_signal: bool = False,
    ) -> None:
        with self._lock:
            if self._completed and not force_after_completion:
                return
            signal_number = self._first_signal
            if signal_number is None:
                return
            self._force_requested = True
            dispatch_cleanup = not self._cleanup_dispatched
            if dispatch_cleanup:
                self._cleanup_dispatched = True
            cleanup_requests = self._cleanup_requests
            call_hard_exit = repeated_signal or not self._hard_exit_started
            if call_hard_exit:
                self._hard_exit_started = True

        if dispatch_cleanup and cleanup_requests is not None:
            try:
                cleanup_requests.put(True)
            except BaseException:
                pass
        if repeated_signal and dispatch_cleanup:
            self._cleanup_finished.wait(FORCED_CLEANUP_GRACE_SECONDS)
        if call_hard_exit:
            self._hard_exit(int(_signal_exit_code(signal_number)))

    def _begin_forced_exit_from_signal(self, *, repeated_signal: bool) -> None:
        """不获取协调器锁的真实 signal handler 强退路径。"""
        signal_number = self._first_signal
        if signal_number is None:
            return
        self._force_requested = True
        dispatch_cleanup = not self._cleanup_dispatched
        if dispatch_cleanup:
            self._cleanup_dispatched = True
        cleanup_requests = self._cleanup_requests
        call_hard_exit = repeated_signal or not self._hard_exit_started
        if call_hard_exit:
            self._hard_exit_started = True

        if dispatch_cleanup and cleanup_requests is not None:
            try:
                cleanup_requests.put(True)
            except BaseException:
                pass
        if repeated_signal and dispatch_cleanup:
            self._cleanup_finished.wait(FORCED_CLEANUP_GRACE_SECONDS)
        if call_hard_exit:
            self._hard_exit(int(_signal_exit_code(signal_number)))

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers_running:
                return
            setup_requests: SimpleQueue[int | None] = SimpleQueue()
            cleanup_requests: SimpleQueue[bool] = SimpleQueue()
            self._setup_requests = setup_requests
            self._cleanup_requests = cleanup_requests
            self._cleanup_finished = Event()
            self._workers_running = True
            setup_thread = Thread(
                target=self._run_setup_worker,
                args=(setup_requests,),
                name="video-auto-editor-cancellation-setup",
                daemon=True,
            )
            cleanup_thread = Thread(
                target=self._run_cleanup_worker,
                args=(cleanup_requests,),
                name="video-auto-editor-force-cleanup",
                daemon=True,
            )
            self._setup_thread = setup_thread
            self._cleanup_thread = cleanup_thread
        try:
            setup_thread.start()
            cleanup_thread.start()
        except BaseException:
            with self._lock:
                self._workers_running = False
            try:
                setup_requests.put(None)
                cleanup_requests.put(False)
            except BaseException:
                pass
            raise

    def _run_setup_worker(
        self,
        requests: SimpleQueue[int | None],
    ) -> None:
        while True:
            signal_number = requests.get()
            if signal_number is None:
                return
            self._source._notify_waiters()
            self._start_watchdog()

    def _run_cleanup_worker(
        self,
        requests: SimpleQueue[bool],
    ) -> None:
        if not requests.get():
            return
        try:
            self._best_effort_cleanup()
        except BaseException:
            # 强退路径不能安全追加诊断，也不得输出可能含敏感内容的 traceback。
            pass
        finally:
            self._cleanup_finished.set()

    def _start_dispatcher(self, read_fd: int) -> None:
        self._dispatcher_stop = Event()
        dispatcher = Thread(
            target=self._run_dispatcher,
            args=(read_fd,),
            name="video-auto-editor-signal-dispatcher",
            daemon=True,
        )
        self._dispatcher_thread = dispatcher
        dispatcher.start()

    def _run_dispatcher(self, read_fd: int) -> None:
        while not self._dispatcher_stop.is_set():
            try:
                payload = os.read(read_fd, 4096)
            except InterruptedError:
                continue
            except OSError:
                return
            if not payload:
                return
            for signal_number in payload:
                if signal_number == 0 or self._dispatcher_stop.is_set():
                    return
                if signal_number in _HANDLED_SIGNALS:
                    self._dispatch_signal(signal_number)

    def _stop_dispatcher(
        self,
        read_fd: int | None,
        write_fd: int | None,
    ) -> None:
        self._dispatcher_stop.set()
        if write_fd is not None:
            try:
                os.write(write_fd, b"\0")
            except OSError:
                pass
            try:
                os.close(write_fd)
            except OSError:
                pass
        dispatcher = self._dispatcher_thread
        if dispatcher is not None and dispatcher is not current_thread():
            try:
                dispatcher.join(timeout=0.2)
            except RuntimeError:
                # start() 失败的线程尚不可 join，但仍须继续关闭 fd 和 workers。
                pass
        if read_fd is not None:
            try:
                os.close(read_fd)
            except OSError:
                pass
        self._dispatcher_thread = None

    def _stop_workers(self) -> None:
        with self._lock:
            if not self._workers_running:
                return
            self._workers_running = False
            setup_requests = self._setup_requests
            cleanup_requests = self._cleanup_requests
            setup_thread = self._setup_thread
            cleanup_thread = self._cleanup_thread
            self._setup_requests = None
            self._cleanup_requests = None
            self._setup_thread = None
            self._cleanup_thread = None
        if setup_requests is not None:
            setup_requests.put(None)
        if cleanup_requests is not None and not self._cleanup_dispatched:
            cleanup_requests.put(False)
        for worker in (setup_thread, cleanup_thread):
            if worker is not None and worker is not current_thread():
                worker.join(timeout=0.2)


def _cancel_timer(timer: _TimerHandle | None) -> None:
    if timer is None:
        return
    try:
        timer.cancel()
    except BaseException:
        # 看门狗撤销失败不能阻止第二信号强退。
        pass
