"""One coordinator per workspace, enforced by an OS lock, not a PID file.

This does not isolate files or stop previously launched external programs.
"""
from pathlib import Path
import os
import threading


class OwnershipError(RuntimeError):
    pass


class WorkspaceLease:
    def __init__(self,workspace):
        self.root=Path(workspace).resolve()
        self.handle=None

    def acquire(self):
        if self.handle is not None:
            return self
        self.root.mkdir(parents=True,exist_ok=True)
        path=self.root/'.engine.lock'
        if path.is_symlink():
            raise OwnershipError('工作区所有权文件不能是符号链接')
        handle=path.open('a+b')
        try:
            if path.stat().st_size==0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise OwnershipError('工作区已有引擎拥有者；先关闭原入口，再连接此入口') from exc
        self.handle=handle
        return self

    def close(self):
        handle,self.handle=self.handle,None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
        finally:
            handle.close()

    def close_after(self,threads):
        """Keep ownership until in-flight coordinator threads actually exit."""
        pending=[t for t in threads if t and t.is_alive() and t is not threading.current_thread()]
        if not pending:
            self.close()
            return
        def finish():
            for thread in pending:
                thread.join()
            self.close()
        threading.Thread(target=finish,name='workspace-owner-release',daemon=True).start()

    def __enter__(self):
        return self.acquire()

    def __exit__(self,*args):
        self.close()
