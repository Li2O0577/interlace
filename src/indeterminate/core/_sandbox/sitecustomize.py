"""Loaded automatically by Python processes that the workbench starts for project code
(see mch/core/sandbox.py). Refuses writes outside the allowed folders and, if configured,
network connections. Kept dependency-free and fast: it runs on every file open."""

import os
import sys


def _install():
    roots = [os.path.realpath(r) for r in os.environ.get("MCH_SANDBOX_ROOTS", "").split(os.pathsep) if r]
    if not roots:
        return
    allow_net = os.environ.get("MCH_SANDBOX_NET", "1") == "1"
    devices = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/shm", "/dev/fd", "/proc/self")
    WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    realpath, fspath, isinstance_, str_, bytes_ = os.path.realpath, os.fspath, isinstance, str, bytes

    def inside(path):
        try:
            p = fspath(path)
        except TypeError:
            return True
        if isinstance_(p, bytes_):
            p = p.decode(errors="replace")
        if not isinstance_(p, str_):
            return True
        p = realpath(p)
        if p.startswith(devices):
            return True
        for r in roots:
            if p == r or p.startswith(r + os.sep):
                return True
        return False

    def deny(path):
        raise PermissionError(f"[MCH 代码执行限制] 不允许修改项目文件夹以外的位置：{path}")

    def hook(event, args):
        if event == "open":
            path, mode, flags = args
            if isinstance_(path, int) or path is None:
                return
            writing = (mode is not None and any(c in mode for c in "wax+")) or (mode is None and flags & WRITE)
            if writing and not inside(path):
                deny(path)
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.utime",
                       "shutil.rmtree", "os.link", "os.symlink"):
            if event in ("os.link", "os.symlink"):
                path = args[1]
            else:
                path = args[0]
            if isinstance_(path, int):
                return
            if not inside(path):
                deny(path)
        elif event in ("os.rename", "shutil.move", "shutil.copyfile", "shutil.copytree"):
            if not inside(args[1]) or (event in ("os.rename", "shutil.move") and not inside(args[0])):
                deny(args[1])
        elif event == "socket.connect" and not allow_net:
            address = args[1]
            if isinstance_(address, (str_, bytes_)):
                return   # a local (unix) socket
            if isinstance_(address, tuple) and address and address[0] in ("127.0.0.1", "::1", "localhost"):
                return
            raise PermissionError("[MCH 代码执行限制] 已禁止代码联网（设置 → 代码执行）；下载数据请用 data.download")

    sys.addaudithook(hook)


_install()
del _install
