"""A watchdog for agents that look busy while nobody works.

An agent's status says ``running`` (or ``waiting_children``) only while its thread is inside a turn. If the
status says so and no turn is in progress for a while (a wait that outlived its turn, a thread that died), the
agent would sit there forever: nothing wakes a thread that thinks it is idle. The watchdog checks every minute
and puts such an agent back on its feet: it restarts a dead thread, or wakes the thread so it looks at its
inbox again (and goes idle if there is nothing to do). Each repair is logged as a notice.

It also ends the pause of an agent the account's usage limit stopped (``hold.until``), once that time has
passed, so a run continues on its own after the limit resets, also across a restart of the workbench.
"""

from __future__ import annotations

import threading
import time

LOOKS_BUSY = ("running", "waiting_children")


class WatchdogMixin:
    WATCH_EVERY = 60      # seconds between checks
    STALE_AFTER = 120     # seconds an agent may look busy with no turn in progress

    def start_watchdog(self) -> None:
        if getattr(self, "_watchdog", None) is not None:
            return

        def loop():
            while not self.closing:
                time.sleep(self.WATCH_EVERY)
                try:
                    self.watch_once()
                except Exception:  # noqa: BLE001 — the watchdog must never stop watching
                    pass
        self._watchdog = threading.Thread(target=loop, daemon=True, name="agent-watchdog")
        self._watchdog.start()

    def watch_once(self, now: float | None = None) -> list[str]:
        """Repair agents that have looked busy without a turn for too long, and resume agents whose usage-limit
        pause is over; returns their names."""
        now = time.time() if now is None else now
        fixed = []
        for st in list(self.states.values()):
            hold = st.rec.get("hold") or {}
            if (not st.stop and st.rec["status"] == "paused" and hold.get("by") == "system" and hold.get("until")
                    and now >= float(hold["until"])):
                self._resume_after_limit(st)
                fixed.append(st.rec["name"])
                continue
            if st.stop or st.rec["status"] not in LOOKS_BUSY or st.working:
                st.stale_since = None
                continue
            st.stale_since = st.stale_since or now
            if now - st.stale_since < self.STALE_AFTER:
                continue
            st.stale_since = None
            alive = st.thread is not None and st.thread.is_alive()
            with st.cv:
                if st.rec["status"] == "waiting_children":
                    self._set_status(st, "running")    # the loop re-reads its inbox, then works or goes idle
                st.cv.notify_all()
            if not alive:
                self._ensure_thread(st)
            fixed.append(st.rec["name"])
            self._emit(st.project_id, "system.notice", {
                "text": f"「{st.rec['name']}」显示在工作，但已有一段时间没有进行中的轮次，已自动唤醒"
                        + ("（工作线程已重启）" if not alive else "")})
        return fixed

    def _resume_after_limit(self, st) -> None:
        with st.cv:
            st.rec["hold"] = None
            st.rec.pop("prev_status", None)
            self._set_status(st, "running", by="system", reason="用量上限已过，自动继续")
            st.cv.notify_all()
        self._ensure_thread(st)
        self._emit(st.project_id, "system.notice", {"text": f"「{st.rec['name']}」的用量上限已过，已自动继续"})
        self._schedule(st.project_id)
