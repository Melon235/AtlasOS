"""Local OS-backed ownership lock for one Atlas workload.

This module deliberately provides process-local/Host-local mutual exclusion only. It is not a
distributed lease, fencing token, heartbeat, or cross-OS lock protocol. The lock is held by an open
file descriptor; process death releases it through the operating system.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import time
from pathlib import Path

_OWNERSHIP_IDENTITY_VERSION = b"atlas-m5-workload-owner-v1"


class SandboxOwnershipError(RuntimeError):
    pass


class SandboxOwnershipBusy(SandboxOwnershipError):
    """Another process currently owns the workload lock."""


class SandboxOwnershipUnavailable(SandboxOwnershipError):
    """The local lock backend or lock directory cannot provide ownership."""


def workload_lock_identity(run_id: str, task_id: str) -> str:
    """Stable, path-safe identity digest; never concatenate raw IDs into a path."""

    encoded = (
        _OWNERSHIP_IDENTITY_VERSION
        + b"\0"
        + str(len(run_id)).encode("ascii")
        + b":"
        + run_id.encode("utf-8")
        + b"\0"
        + str(len(task_id)).encode("ascii")
        + b":"
        + task_id.encode("utf-8")
    )
    return hashlib.sha256(encoded).hexdigest()


class WorkloadOwnershipLock:
    """One exclusive OS file lock for ``(run_id, task_id)``.

    The lock is not considered held by checking for the file's existence. The descriptor and the
    OS lock remain open until ``release`` or process exit.
    """

    def __init__(
        self,
        state_root: str | Path,
        *,
        run_id: str,
        task_id: str,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self.state_root = Path(state_root)
        self.run_id = run_id
        self.task_id = task_id
        self.identity = workload_lock_identity(run_id, task_id)
        self.lock_path = self.state_root / f"workload-{self.identity}.lock"
        self.poll_interval_seconds = poll_interval_seconds
        self._fd: int | None = None
        self._backend = ""

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _open_fd(self) -> int:
        fd: int | None = None
        try:
            self.state_root.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            return fd
        except OSError as error:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise SandboxOwnershipUnavailable(
                f"cannot open local workload lock {self.lock_path}: {error}"
            ) from error

    def acquire(self, *, timeout_seconds: float = 0.0) -> WorkloadOwnershipLock:
        if self._fd is not None:
            return self
        fd = self._open_fd()
        try:
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            while True:
                if self._try_lock(fd):
                    self._fd = fd
                    return self
                if time.monotonic() >= deadline:
                    raise SandboxOwnershipBusy(
                        f"workload {self.run_id!r}/{self.task_id!r} is owned by another process"
                    )
                # Reopen between attempts. On Windows a descriptor that observed a
                # conflicting byte-range lock can remain unusable for a retry even after
                # the previous owner closes its descriptor.
                os.close(fd)
                time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))
                fd = self._open_fd()
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise

    def _try_lock(self, fd: int) -> bool:
        if sys.platform == "win32":
            try:
                import msvcrt
            except ImportError as error:
                raise SandboxOwnershipUnavailable(
                    f"Windows msvcrt lock backend unavailable: {error}"
                ) from error
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                self._backend = "msvcrt.locking"
                return True
            except OSError:
                return False
        try:
            import fcntl
        except ImportError as error:
            raise SandboxOwnershipUnavailable(
                f"POSIX fcntl lock backend unavailable: {error}"
            ) from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._backend = "fcntl.flock"
            return True
        except BlockingIOError:
            return False
        except OSError as error:
            if error.errno in (11, 13, 35):  # EAGAIN / EACCES / EWOULDBLOCK
                return False
            raise SandboxOwnershipUnavailable(f"POSIX workload lock failed: {error}") from error

    def assert_held(self) -> None:
        if self._fd is None:
            raise SandboxOwnershipError(
                f"local ownership for workload {self.run_id!r}/{self.task_id!r} is not held"
            )

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> WorkloadOwnershipLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.release()


__all__ = [
    "SandboxOwnershipBusy",
    "SandboxOwnershipError",
    "SandboxOwnershipUnavailable",
    "WorkloadOwnershipLock",
    "workload_lock_identity",
]
