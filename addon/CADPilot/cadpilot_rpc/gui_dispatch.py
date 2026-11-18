"""GUI-thread task dispatch for the RPC server.

The XML-RPC server answers on its own threads. FreeCAD APIs that touch the GUI
or the document tree must run in the main GUI thread. This module owns the
queue that ferries wrapped callables onto the GUI thread and the helper that
RPC handlers use to invoke them.

Robustness and performance guarantees:

1. Per-call response queues: each ``dispatch_to_gui`` call owns its own
   ``queue.Queue``. A timeout in one call can never corrupt the response for
   a subsequent call.
2. Immediate wake via Qt signal: ``dispatch_to_gui`` emits a signal from the
   RPC thread; the GUI thread processes the task immediately rather than
   waiting for the next 500 ms heartbeat tick. The heartbeat is one persistent
   QTimer started and stopped with the server, kept as a fallback for a lost
   wake and for a queue that was held back by the guards below.
3. Mouse-button guard: ``process_gui_tasks`` skips the current tick while
   mouse buttons are held so MCP tasks cannot interrupt 3D navigation drags,
   and re-checks the guard BETWEEN queued tasks so a press that begins
   mid-drain pauses the queue instead of starving user input for the whole
   backlog. On Windows the OS physical button state (GetAsyncKeyState) is
   authoritative in both directions — Qt's mouseButtons() is only refreshed
   when the event loop delivers button events, which is stale exactly while
   the queue is busy, so consulting it alone misses fresh presses and late
   releases (the race that let tasks punch through mid-drag). Off Windows,
   phantom/stuck states are filtered by a static-state time cap and a
   hold-duration cap — see ``_user_holding_button``.
4. Exception isolation: exceptions inside a task are caught, logged, and
   returned as error strings; they never kill the dispatch loop.
"""

import contextlib
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import FreeCADGui
from PySide import QtCore, QtGui, QtWidgets

from cadpilot_rpc import dbglog

logger = dbglog.get_logger("gui")

_rpc_request_queue: "queue.Queue[Any]" = queue.Queue()
#: Tasks that must run even while a modal dialog (or a drag) holds the normal
#: queue back. Only dismissing that dialog belongs here: it is the one operation
#: that has to work precisely when everything else is held back, and it is how a
#: client with no GUI (Claude Code, opencode, ...) recovers a session wedged on
#: FreeCAD's document-recovery dialog.
_force_queue: "queue.Queue[Any]" = queue.Queue()
_processing = False  # re-entrancy guard: True while process_gui_tasks is draining
_processing_since: float = 0.0  # wall-clock time when _processing became True

# Phantom-input detection for the mouse-button guard — only needed where the
# OS cannot report the physical button state (i.e. off Windows; on win32 the
# ground truth makes phantoms impossible to mistake, see _user_holding_button).
# A stuck mouseButtons() state (e.g. after a background launch or RDP session)
# is *static*: same buttons and same cursor position for a long stretch. Real
# drags always move the cursor or change buttons. The guard treats a state
# that has been identical for this many seconds as phantom and processes the
# queue anyway. Measured in TIME, not ticks: the guard is also evaluated
# between queued tasks now, so evaluations are no longer ~500 ms apart.
_PHANTOM_STATIC_SECONDS = 10.0
_last_held_state: "tuple | None" = None
_held_static_since: "float | None" = None

# The static cap alone is NOT enough: a phantom stuck button PLUS a live
# cursor (the user keeps moving the mouse over the active window — normal
# while inspecting a model between run steps) resets the static timer on
# every evaluation, so the queue would defer forever (the recurring wedge).
# The hold-duration cap bounds that: the same nonzero mask held continuously
# longer than this is treated as phantom even with a moving cursor. Both caps
# are heuristic-path only — on win32 the OS says whether the button is REALLY
# down, and a real hold must never be capped: punching a task through a long
# inspection drag at the 15 s mark is precisely the freeze users feel.
_PHANTOM_HOLD_SECONDS = 15.0
_held_mask: "int | None" = None
_held_since: float = 0.0

# Why the queue was last deferred, and for how long. Written on the GUI thread by
# process_gui_tasks, read on the RPC thread by the timeout path — a plain str/float
# is atomic enough in CPython and this is only a diagnosis.
#
# Without it the timeout message blamed the waker unconditionally, which is wrong
# exactly when a human is interacting: the heartbeat is healthy, the queue simply
# must not be drained during a drag, an open context menu or a modal dialog. The
# reported symptom ("it happens when I operate FreeCAD at the same time as the
# model; the process CPU barely moves") is that case, not a dead chain.
_DEFER_LABELS = {
    "button": "the user is holding a mouse button in the FreeCAD window",
    "popup": "a popup menu is open",
    "modal": "a modal dialog is open",
}
_DEFER_WARN_SECONDS = 10.0
_defer_reason: "str | None" = None
_defer_since: float = 0.0
_defer_warned = False

# How long a dispatch waits before it reports user-interaction back-pressure
# instead of waiting out its whole timeout. The guard will not drain the queue
# while the interaction lasts, so a 60s wait only turns an already-known outcome
# into a late one: the caller sat in silence for a minute to be told "release
# the mouse button". Long enough that an ordinary click (a few hundred ms)
# still completes normally.
_USER_HOLD_GRACE = 2.0
_MISSING = object()

# Calls whose caller was told "timed out" while their task was already running
# on the GUI thread, and which then ran to the end anyway: the change, its undo
# entry and its journal step landed after the error (live-caught: LateBox). The
# RPC layer hands these out on the next reply as ``late_completions``.
_late_completions: list[dict] = []
_late_lock = threading.Lock()
_LATE_MAX = 20

# The RPC method each request thread is serving, for the late-completion record.
_call = threading.local()


def set_call_method(method: str | None) -> None:
    _call.method = method


def take_late_completions() -> list[dict]:
    """Every late completion recorded since the last call, oldest first."""
    with _late_lock:
        taken = list(_late_completions)
        _late_completions.clear()
    return taken


def _record_late_completion(
    request: int | None, method: str | None, queued_at: float, timeout: float, error: str | None
) -> None:
    finished_after = time.monotonic() - queued_at
    entry: dict[str, Any] = {
        "request": request,
        "method": method or "unknown",
        "outcome": "raised" if error else "completed",
        "timed_out_after_s": round(float(timeout), 1),
        "finished_after_s": round(finished_after, 1),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if error:
        entry["error"] = error[:500]
        entry["note"] = (
            "its caller was told it timed out; it then failed, so a transactional call "
            "rolled its changes back"
        )
    else:
        entry["note"] = (
            "its caller was told it timed out, but it ran to completion afterwards: its "
            "document changes, undo entry and journal step are in place, so do not repeat it"
        )
    logger.warning(
        "late completion: req#%s %s %s %.1fs after it was queued; its caller had been "
        "told it timed out after %.1fs",
        request,
        entry["method"],
        entry["outcome"],
        finished_after,
        timeout,
    )
    with _late_lock:
        _late_completions.append(entry)
        del _late_completions[:-_LATE_MAX]


def _queue_depth() -> int:
    """Queued tasks that will still run.

    A caller that timed out leaves its task in the queue, marked cancelled, and
    the drain skips it; counting those made a queue that would run nothing look
    backed up.
    """
    with _rpc_request_queue.mutex:
        pending = list(_rpc_request_queue.queue)
    return sum(1 for task in pending if not _is_cancelled(task))


def _is_cancelled(task: Any) -> bool:
    flag = getattr(task, "cancelled", None)
    return flag is not None and flag.is_set()


def _note_defer(reason: str) -> None:
    """Record a deferral, warning once when it outlasts _DEFER_WARN_SECONDS.

    The warning is what makes a stalled queue visible in get_addon_log while it
    is happening — otherwise the only trace is a client-side timeout.
    """
    global _defer_reason, _defer_since, _defer_warned
    now = time.monotonic()
    if _defer_reason != reason:
        _defer_reason = reason
        _defer_since = now
        _defer_warned = False
        return
    if not _defer_warned and now - _defer_since >= _DEFER_WARN_SECONDS:
        _defer_warned = True
        logger.warning(
            "GUI queue deferred for %.1fs by %s (queue depth %d) — RPC calls will time out "
            "until it clears; the heartbeat is fine, this is back-pressure",
            now - _defer_since,
            _DEFER_LABELS.get(reason, reason),
            _queue_depth(),
        )


def _clear_defer() -> None:
    global _defer_reason, _defer_warned
    _defer_reason = None
    _defer_warned = False


#: What the guards would say RIGHT NOW, refreshed on every GUI tick. The RPC
#: thread reads this (never Qt widget state itself, which is not safe off the
#: GUI thread), so a diagnosis run can name an open modal dialog before any call
#: has been held back — with an idle queue nothing is deferred yet, and the
#: recorded reason alone would answer "no back-pressure" while a dialog sits in
#: front of the window (live-caught exactly that way).
_guard_state: str = ""


def _probe_guard_state() -> None:
    global _guard_state
    try:
        app = QtWidgets.QApplication.instance()
        if app is None:
            _guard_state = ""
        elif app.activeModalWidget() is not None:
            _guard_state = "modal"
        elif app.activePopupWidget() is not None:
            _guard_state = "popup"
        elif _user_holding_button():
            _guard_state = "button"
        else:
            _guard_state = ""
    except Exception:
        _guard_state = ""


def dismiss_blocking_dialog(timeout: float = 5.0) -> dict:
    """Close the modal dialog that is holding the GUI queue back (Cancel).

    The one operation that must work WHILE the queue is held back: a client with
    no way to operate the GUI (Claude Code, opencode, any MCP client) otherwise
    has no recovery from FreeCAD's document-recovery dialog, which is shown
    after an unclean shutdown and blocks every dispatched call until someone
    clicks it. Cancel semantics only: the dialog is closed the way its own
    Cancel button would, so nothing is ever confirmed.
    """

    def task() -> dict:
        app = QtWidgets.QApplication.instance()
        widget = app.activeModalWidget() if app is not None else None
        if widget is None:
            return {
                "success": True,
                "dismissed": None,
                "note": "no modal dialog is open; nothing to dismiss",
            }
        title, kind, closed = "", type(widget).__name__, False
        with contextlib.suppress(Exception):
            title = str(widget.windowTitle() or "")
        with contextlib.suppress(Exception):
            widget.reject()  # == the dialog's Cancel button
            closed = True
        if not closed:
            with contextlib.suppress(Exception):
                widget.close()
                closed = True
        return {
            "success": closed,
            "dismissed": {"title": title, "type": kind} if closed else None,
            "note": (
                "closed the dialog with Cancel semantics; nothing was confirmed"
                if closed
                else "the dialog refused to close (it overrides Cancel/close)"
            ),
        }

    res = dispatch_to_gui(task, timeout=timeout, force=True)
    if isinstance(res, dict):
        return res
    return {"success": False, "error": f"could not dismiss the dialog: {res!r}"}


def backpressure() -> dict:
    """Why the GUI queue is not draining, in the shape the RPC endpoint reports.

    Deliberately NOT dispatched: it must answer while the queue is held back or
    the GUI thread is busy, which is exactly when a caller needs the reason. A
    held-back queue (mouse button / modal dialog / long recompute) and a dead
    waker look identical from outside the process and want opposite advice.
    """
    reason = _defer_reason or _guard_state or None
    return {
        "success": True,
        "defer_reason": reason,
        "defer_label": _DEFER_LABELS.get(reason, reason) if reason else "",
        # Only a RECORDED deferral has a start time. One merely observed on the
        # tick has none, and `now - 0.0` printed a four-day duration.
        "deferred_s": round(time.monotonic() - _defer_since, 1) if _defer_reason else 0.0,
        "processing": bool(_processing),
        "processing_s": round(time.monotonic() - _processing_since, 1) if _processing else 0.0,
        "queue_depth": _queue_depth(),
    }


def _physical_buttons_down() -> "int | None":
    """OS-level physical mouse-button mask on Windows; None elsewhere.

    Bits match Qt LeftButton/RightButton/MiddleButton (1/2/4). Honors the
    left-handed swap setting. Never raises — diagnostics must not break
    dispatch; on any failure returns None (heuristic paths take over).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        u = ctypes.windll.user32
        if u.GetSystemMetrics(23):  # SM_SWAPBUTTON: left-handed mouse
            vk_l, vk_r = 0x02, 0x01
        else:
            vk_l, vk_r = 0x01, 0x02
        mask = 0
        if u.GetAsyncKeyState(vk_l) & 0x8000:
            mask |= 1
        if u.GetAsyncKeyState(vk_r) & 0x8000:
            mask |= 2
        if u.GetAsyncKeyState(0x04) & 0x8000:  # VK_MBUTTON
            mask |= 4
        return mask
    except Exception:
        return None


def _reset_hold_trackers() -> None:
    """Forget all held-button tracking state (heuristic path bookkeeping)."""
    global _last_held_state, _held_static_since, _held_mask
    _last_held_state = None
    _held_static_since = None
    _held_mask = None


def _user_holding_button() -> bool:
    """True while a real user is holding a mouse button in the active window.

    On Windows the OS physical state is authoritative in BOTH directions.
    Qt's mouseButtons() is only updated when the event loop delivers button
    events, so it is stale exactly while the queue is busy: a fresh press is
    invisible until the running task finishes (tasks used to start mid-drag)
    and a release is invisible too (defers outlasted the drag). Consulting
    GetAsyncKeyState only to REJECT phantom holds — the old design — fixed
    the starvation direction but left the interrupt-the-user direction racy.
    A nonzero physical mask with the FreeCAD window active is a REAL hold:
    never capped, because capping it is what punched a task through a long
    inspection drag at the 15 s mark.

    Off Windows there is no ground truth, so the Qt state decides, with two
    phantom filters (static-state time cap, hold-duration cap) so a stuck
    button state can never starve the queue.
    """
    global _last_held_state, _held_static_since, _held_mask, _held_since
    physical = _physical_buttons_down()
    if physical is not None:
        if physical == 0 or not FreeCADGui.getMainWindow().isActiveWindow():
            # OS says no button is down (any Qt-held state is a stale phantom),
            # or the hold belongs to some other window. The inactive branch
            # must reset too: a stale _held_since would otherwise trip the
            # hold cap the moment the user comes back mid-hold.
            _reset_hold_trackers()
            return False
        return True
    buttons = QtWidgets.QApplication.mouseButtons()
    if buttons == QtCore.Qt.NoButton:
        _reset_hold_trackers()
        return False
    if not FreeCADGui.getMainWindow().isActiveWindow():
        _reset_hold_trackers()  # same stale-_held_since trap as above
        return False
    pos = QtGui.QCursor.pos()
    state = (buttons, pos.x(), pos.y())
    now = time.monotonic()
    if state != _last_held_state:
        _last_held_state = state
        _held_static_since = now
    if buttons != _held_mask:
        _held_mask = buttons
        _held_since = now
    if _held_static_since is not None and now - _held_static_since >= _PHANTOM_STATIC_SECONDS:
        logger.debug("mouse guard: motionless phantom cap hit (%.1fs)", now - _held_static_since)
        return False  # motionless phantom cap
    held_for = now - _held_since
    if held_for >= _PHANTOM_HOLD_SECONDS:
        logger.debug("mouse guard: hold-duration cap hit (%.1fs)", held_for)
        return False  # continuous-hold cap (phantom + live cursor)
    return True


class _WakeSignal(QtCore.QObject):
    """Qt signal bridge for cross-thread GUI-task wakeup.

    Must be created on the GUI thread (``init_waker``). Emitting from the
    RPC thread is safe: Qt delivers the connection with ``QueuedConnection``,
    so the slot always fires in the GUI thread's event loop.
    """

    _sig = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._sig.connect(self._on_wake, QtCore.Qt.QueuedConnection)

    def wake(self) -> None:
        self._sig.emit()

    def _on_wake(self) -> None:
        process_gui_tasks()


_waker: "_WakeSignal | None" = None

_HEARTBEAT_MS = 500
# One persistent timer, not a singleShot chain: a chain re-armed itself only at
# the end of a tick, and a tick that fired inside a running task (any task that
# pumps events for longer than 500 ms) returned at the re-entrancy guard
# without re-arming, so the heartbeat died for good and queued work nobody
# woke never ran.
_heartbeat: "QtCore.QTimer | None" = None


def init_waker() -> None:
    """Create the wake-signal bridge and start the heartbeat (GUI thread)."""
    global _waker, _heartbeat
    _waker = _WakeSignal()
    if _heartbeat is None:
        _heartbeat = QtCore.QTimer()
        _heartbeat.setInterval(_HEARTBEAT_MS)
        _heartbeat.timeout.connect(process_gui_tasks)
    _heartbeat.start()


def cleanup_waker() -> None:
    """Stop the heartbeat and release the wake-signal bridge on server stop."""
    global _waker, _heartbeat
    timer, _heartbeat = _heartbeat, None
    _waker = None
    if timer is None:
        return
    if threading.current_thread() is threading.main_thread():
        timer.stop()
    else:
        # A QTimer can only be stopped from its own thread; the queued task
        # also keeps the timer alive until it has been stopped there.
        _rpc_request_queue.put(timer.stop)


def _flush_gui_events(delay_ms: int = 20) -> None:
    FreeCADGui.updateGui()
    app = QtWidgets.QApplication.instance()
    if app is None:
        return

    # ExcludeUserInputEvents: skip mouse/keyboard events to avoid re-entrancy
    # with ongoing navigation. ExcludeSocketNotifiers keeps network I/O out.
    flags = QtCore.QEventLoop.ExcludeUserInputEvents | QtCore.QEventLoop.ExcludeSocketNotifiers
    app.processEvents(flags, delay_ms)
    if delay_ms > 0:
        QtCore.QThread.msleep(delay_ms)
        app.processEvents(flags, delay_ms)


def _drain_on_stack() -> bool:
    """True when this tick runs inside a drain, i.e. a task is pumping events."""
    frame = sys._getframe(2)
    while frame is not None:
        code = frame.f_code
        if code.co_name == "process_gui_tasks" and code.co_filename.endswith("gui_dispatch.py"):
            return True
        frame = frame.f_back
    return False


def _recover_dead_drain() -> None:
    """Clear the busy flag of a drain that is no longer running.

    FreeCAD can throw a C++ exception straight through the Python stack (seen on
    1.1.4: ``Units.Quantity("1 SpigotR")`` raised "Unit mismatch in plus
    operation", reported only as "Unhandled Base::Exception caught in
    GUIApplication::notify"). No ``finally`` runs, so ``_processing`` stayed True
    with nothing on the stack, every later tick returned at the re-entrancy
    guard, and each call timed out "behind another call" until FreeCAD was
    restarted.
    """
    global _processing
    logger.error(
        "a GUI task ended without returning (a C++ exception escaped Python after %.1fs); "
        "its caller got no reply. Clearing the busy flag so the queue runs again",
        time.monotonic() - _processing_since,
    )
    _processing = False
    app = QtWidgets.QApplication.instance()
    if app is not None and app.overrideCursor() is not None:
        app.restoreOverrideCursor()
    with contextlib.suppress(Exception):
        FreeCADGui.getMainWindow().statusBar().clearMessage()


def process_gui_tasks() -> None:
    """Drain queued GUI-thread callables (wake signal and heartbeat tick).

    Skips the normal queue when any mouse button is held (e.g., 3D navigation
    drag) or when already executing a task (re-entrancy guard). The guard
    prevents ``doc.recompute()`` or ``processEvents()`` inside a task from
    triggering a nested ``process_gui_tasks`` call that corrupts FreeCAD state.
    Forced tasks and the guard-state probe run before both guards.
    """
    global _processing, _processing_since
    # Refresh the guard state on EVERY tick, empty queue and nested ticks
    # included: it is the RPC thread's only safe view of whether a dialog/popup
    # is in the way, and a modal opened by the running task is only visible
    # from a tick its exec() loop delivers.
    _probe_guard_state()
    # Forced tasks run BEFORE (and regardless of) the interaction guards AND the
    # re-entrancy guard: dismissing the dialog that is holding the queue back
    # must work while it holds it, or no client without a GUI can ever recover.
    # A modal opened inside a running task is that task's own nested exec()
    # loop, which is what delivers this tick, and reject() is what lets the
    # task continue.
    while True:
        try:
            forced = _force_queue.get_nowait()
        except queue.Empty:
            break
        try:
            forced()
        except Exception as e:
            logger.error(
                "unhandled exception in forced GUI task: %s: %s",
                type(e).__name__,
                e,
                exc_info=True,
            )
    if _processing:
        if _drain_on_stack():
            return  # re-entrant call from processEvents inside a task; skip
        _recover_dead_drain()

    try:
        if _rpc_request_queue.empty():
            _clear_defer()
            return  # nothing queued; skip cursor/status-bar churn on idle heartbeat ticks
        if _user_holding_button():
            # user is dragging in the active window; defer to next tick.
            # (Phantom/stuck button states are filtered out inside
            # _user_holding_button — they must not starve the queue.)
            _note_defer("button")
            logger.debug("mouse guard: deferring queue (real drag in the active window)")
            return
        if QtWidgets.QApplication.activePopupWidget() is not None:
            _note_defer("popup")
            return  # context menu or popup open; defer to next tick
        if QtWidgets.QApplication.activeModalWidget() is not None:
            _note_defer("modal")
            return  # modal dialog open; defer to next tick

        _processing = True
        _processing_since = time.monotonic()
        app = QtWidgets.QApplication.instance()
        try:
            status_bar = FreeCADGui.getMainWindow().statusBar()
        except Exception:
            status_bar = None

        if app is not None:
            app.setOverrideCursor(QtCore.Qt.WaitCursor)
        if status_bar is not None:
            status_bar.showMessage("CADPilot: processing…")
        try:
            ran_one = False
            while not _rpc_request_queue.empty():
                if ran_one:
                    # Re-check the interaction guards BETWEEN tasks: the checks
                    # above only run when a drain starts, so without this a
                    # press that begins mid-drain (or a dialog a task opened)
                    # cannot pause the queue and user input starves for the
                    # whole backlog. Pausing returns to the event loop, which
                    # delivers the pending input; the 500 ms heartbeat (and the
                    # next dispatch's wake) resumes the drain once it clears.
                    if _user_holding_button():
                        _note_defer("button")
                        logger.debug("mouse guard: user input mid-drain; pausing queue")
                        return
                    if QtWidgets.QApplication.activePopupWidget() is not None:
                        _note_defer("popup")
                        return
                    if QtWidgets.QApplication.activeModalWidget() is not None:
                        _note_defer("modal")
                        return
                task = _rpc_request_queue.get()
                try:
                    task()
                except Exception as e:
                    logger.error(
                        "unhandled exception in GUI task: %s: %s",
                        type(e).__name__,
                        e,
                        exc_info=True,
                    )
                ran_one = True
            _clear_defer()  # drained: any earlier deferral is over
        finally:
            if app is not None:
                app.restoreOverrideCursor()
            if status_bar is not None:
                status_bar.clearMessage()
    finally:
        _processing = False


def post_to_gui(task: Callable[[], Any]) -> None:
    """Queue ``task`` for the GUI thread without waiting for it or its result."""

    def _run() -> None:
        try:
            task()
        except Exception as e:
            logger.warning("posted GUI task raised %s: %s", type(e).__name__, e)

    _rpc_request_queue.put(_run)
    waker = _waker
    if waker is not None:
        waker.wake()


def dispatch_to_gui(
    task: Callable[[], Any],
    timeout: float = 60,
    force: bool = False,
    report_held_back: bool = True,
) -> Any:
    """Run ``task`` on the GUI thread and return its result.

    Uses a per-call response queue so a timeout in one call never corrupts
    the response for a subsequent call. Wakes the GUI thread immediately via
    a Qt signal instead of waiting for the next 500 ms heartbeat.

    ``force=True`` puts the task on the forced queue, which the drain runs
    BEFORE the interaction guards: only an operation that must work while the
    queue is deliberately held back (dismissing the modal dialog that holds it)
    belongs there.

    ``report_held_back=False`` waits out the whole timeout while the user holds
    the queue back instead of reporting it after ``_USER_HOLD_GRACE``: right for
    a background worker, which has nobody to tell.

    On timeout the queued task is cancelled: if it has not started yet it
    will never run, so a caller retrying after a timeout cannot trigger a
    double execution. A task already running on the GUI thread cannot be
    interrupted: the error says it may still complete, and a completion is
    recorded for ``take_late_completions``.

    Returns the task's return value on success, an error string if the task
    raises, or ``{"success": False, "error": ...}`` on timeout.
    """
    response_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
    cancelled = threading.Event()
    started = threading.Event()
    # The task's start and its hand-over of the result each race the caller's
    # give-up; under one lock, a task that starts (or finishes) in that instant
    # is seen by the caller, and one that finishes later is recorded as late
    # instead of being reported as applied by nobody.
    handoff = threading.Lock()
    # Captured HERE, on the RPC thread: the GUI thread has no request id of its
    # own, so this is what keeps a call's deferred work correlatable with the
    # request that caused it.
    inherited_request = dbglog.request_id()
    method = getattr(_call, "method", None)

    def _wrapped() -> None:
        with handoff:
            if cancelled.is_set():
                return  # caller timed out and went away; don't run a stale task
            started.set()
        dbglog.set_request_id(inherited_request)
        try:
            error = None
            try:
                res = task()
            except Exception as e:
                logger.error("GUI task raised %s: %s", type(e).__name__, e, exc_info=True)
                res = error = f"{type(e).__name__}: {e}"
            with handoff:
                late = cancelled.is_set()
                if not late:
                    response_queue.put(res)
            if late:
                _record_late_completion(inherited_request, method, queued_at, timeout, error)
        finally:
            dbglog.clear_request_id()

    _wrapped.cancelled = cancelled
    queued_at = time.monotonic()
    (_force_queue if force else _rpc_request_queue).put(_wrapped)
    waker = _waker  # read once: a concurrent stop may clear the global
    if waker is not None:
        waker.wake()  # immediate wake via Qt signal (thread-safe)

    deadline = queued_at + timeout
    result: Any = _MISSING
    deferred_reason: str | None = None
    while result is _MISSING:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = response_queue.get(timeout=min(remaining, _USER_HOLD_GRACE))
        except queue.Empty:
            # _defer_reason only says a defer was RECORDED since the last clear;
            # _clear_defer runs after the drain loop ends and heartbeat ticks
            # return early at the _processing guard, so while a task is actually
            # running the flag is stale. Without the _processing check, a call
            # that lands during a brief hold and then runs longer than the grace
            # slice is misreported as "user holding a mouse button" while the
            # work completes in FreeCAD — and a retry would duplicate it.
            if not force and report_held_back and _defer_reason is not None and not _processing:
                # The guards are holding the queue back on purpose and will keep
                # doing so while the interaction lasts, so waiting out the rest
                # of the timeout cannot succeed — report the actionable reason
                # now. A short click still lands inside _USER_HOLD_GRACE above.
                deferred_reason = _defer_reason
                break
            # Otherwise keep waiting: a busy GUI thread finishes on its own.
    if result is not _MISSING:
        waited = (time.monotonic() - queued_at) * 1000
        if waited > 1000:
            logger.warning("GUI task waited %.1fms (queue depth %d)", waited, _queue_depth())
        else:
            # INFO, not DEBUG: this is the line that proves where a call spent
            # its time (RPC thread vs. GUI queue), so it has to be there by
            # default. The mouse-guard deferrals below stay DEBUG — they repeat
            # every 500ms tick during a drag.
            logger.info("GUI task ran after %.1fms", waited)
        return result

    # Timed out (or gave up early on user interaction): diagnose why.
    with handoff:
        cancelled.set()  # a not-yet-started task must not run after we give up
        running = started.is_set()
        with contextlib.suppress(queue.Empty):
            return response_queue.get_nowait()  # it finished in the last instant
    if running:
        busy = (
            f" (GUI thread busy for {time.monotonic() - _processing_since:.1f}s)"
            if _processing
            else ""
        )
        logger.error(
            "GUI dispatch timed out after %ss while its task was running%s; it may still complete",
            timeout,
            busy,
        )
        return {
            "success": False,
            "may_still_complete": True,
            "error": (
                f"GUI dispatch timed out after {timeout}s while the call was still running "
                f"on the GUI thread{busy}. It was not stopped and may still complete: its "
                "document changes, undo entry and journal step would then appear after this "
                "error. Inspect the document (get_objects, step_control status) before "
                "retrying; a completion is reported on a later reply as late_completions. "
                "Use execute_code_async for heavy OCCT operations."
            ),
        }
    if deferred_reason is not None:
        # The guards are holding the queue back on purpose: the user is
        # mid-interaction. Nothing is broken and nothing needs restarting.
        label = _DEFER_LABELS.get(deferred_reason, deferred_reason)
        deferred_for = time.monotonic() - _defer_since
        logger.error(
            "GUI dispatch reported after %.1fs — %s (queue depth %d, deferred %.1fs); "
            "back-pressure from user interaction, not a dead dispatcher",
            _USER_HOLD_GRACE,
            label,
            _queue_depth(),
            deferred_for,
        )
        return {
            "success": False,
            "error": (
                f"CADPilot could not act on the document within {_USER_HOLD_GRACE:.0f}s because "
                f"{label}. The queued work is held back deliberately while the user interacts, "
                "so nothing is stuck and this call was cancelled before it ran: close the "
                "dialog or menu (or release the mouse button) and retry; the queue drains by "
                "itself. A modal dialog can also be closed for you by the diagnose tool with "
                "dismiss=true, which is not held back."
            ),
        }
    if _processing:
        busy_for = time.monotonic() - _processing_since
        hint = (
            f" waiting behind another call (GUI thread has been busy for {busy_for:.1f}s — "
            "consider execute_code_async for heavy OCCT operations)"
        )
        logger.error("GUI dispatch timed out after %ss%s", timeout, hint)
        return {
            "success": False,
            "error": (
                f"GUI dispatch timed out after {timeout}s{hint}. This call was cancelled "
                "before it started, so nothing was applied."
            ),
        }
    if _defer_reason is not None:
        # Defensive: the deferral started in the last moments of the wait.
        label = _DEFER_LABELS.get(_defer_reason, _defer_reason)
        logger.error(
            "GUI dispatch timed out after %ss — %s (queue depth %d)",
            timeout,
            label,
            _queue_depth(),
        )
        return {
            "success": False,
            "error": (
                f"GUI dispatch timed out after {timeout}s because {label} — CADPilot must "
                "not act on the document mid-interaction, so the queued work is held back "
                "and this call was cancelled before it ran. Nothing is stuck: close the "
                "dialog or menu (or release the mouse button) and retry; the queue drains "
                "by itself."
            ),
        }
    # Idle GUI thread + timeout means the waker or the heartbeat is gone,
    # not that FreeCAD is busy — the failure mode that used to wedge the
    # addon with nothing in any log to show for it.
    logger.error(
        "GUI dispatch timed out after %ss with an idle GUI thread (queue depth %d) "
        "— the waker and its heartbeat timer may be gone",
        timeout,
        _queue_depth(),
    )
    return {
        "success": False,
        "error": (
            f"GUI dispatch timed out after {timeout}s: nothing is holding the queue and "
            "the GUI thread is idle, so the waker and its heartbeat timer may be gone "
            "(gui_dispatch reloaded outside restart_rpc_server). Restart them with "
            "execute_code_async (its worker needs no GUI dispatch); see the addon "
            "section of AGENTS.md."
        ),
    }
