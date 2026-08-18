"""Server-side write lock for solar_settings.html -- so two people (or a
stray script) can't both be "armed" and send conflicting commands.

"Arm" used to be pure client-side JS state, which cannot stop a SECOND
browser tab from also arming and writing -- nothing about it was visible
to, or enforced by, the poller. This makes the lock real: one session ID
holds it at a time, and app/poller.py's command handler refuses any
non-dry-run write from a session that doesn't currently hold it.

In-memory only, held by the same eg4poll process that already validates
and executes writes -- no new service, no persistence needed. It resets on
every restart (including the one a config save already triggers), which is
fine: nobody stays armed across a restart today either, client-side.

Single-threaded asyncio, no `await` inside any method here, so plain
attribute reads/writes are already atomic with respect to the event loop --
no lock-around-the-lock needed.
"""

import time

ARM_TTL_S = 300.0  # 5 minutes -- matches solar_settings.html's existing auto-disarm


class ArmLock:
    def __init__(self, ttl: float = ARM_TTL_S):
        self.ttl = ttl
        self._session_id: str | None = None
        self._expires_at: float = 0.0

    def _expire_if_needed(self):
        if self._session_id is not None and time.time() > self._expires_at:
            self._session_id = None
            self._expires_at = 0.0

    def try_arm(self, session_id: str) -> dict:
        """Claim the lock, or renew it if the caller already holds it.
        Explicit action only -- never called just to check status, so this
        is the only place the TTL gets extended."""
        if not session_id:
            return {"ok": False, "error": "missing session_id"}
        self._expire_if_needed()
        if self._session_id is None or self._session_id == session_id:
            self._session_id = session_id
            self._expires_at = time.time() + self.ttl
            return {"ok": True, "expires_in": self.ttl}
        return {"ok": False, "error": "armed by another session",
                "expires_in": round(self._expires_at - time.time(), 1)}

    def disarm(self, session_id: str) -> dict:
        """Release the lock. Idempotent if nothing is armed; refuses to
        release a lock held by someone else."""
        self._expire_if_needed()
        if self._session_id is None:
            return {"ok": True}
        if self._session_id != session_id:
            return {"ok": False, "error": "armed by another session -- cannot disarm"}
        self._session_id = None
        self._expires_at = 0.0
        return {"ok": True}

    def status(self, session_id: str | None) -> dict:
        """Read-only -- never renews the TTL, so periodic UI polling can't
        accidentally keep a lock alive forever."""
        self._expire_if_needed()
        armed = self._session_id is not None
        return {
            "armed": armed,
            "mine": armed and self._session_id == session_id,
            "expires_in": round(max(0.0, self._expires_at - time.time()), 1) if armed else 0,
        }

    def check(self, session_id: str | None) -> bool:
        """True if session_id currently holds the lock. Called by the
        command handler before executing any real (non-dry-run) write --
        the actual enforcement point, everything else here is bookkeeping."""
        self._expire_if_needed()
        return bool(session_id) and self._session_id == session_id
