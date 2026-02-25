import os

# Work around sandboxed filesystems where os.mkdir(path, mode=0o700) yields a non-writable directory.
# On standard Windows/NTFS, the mode argument is effectively ignored.
if os.name == 'nt':
    _orig_mkdir = os.mkdir

    def _patched_mkdir(path, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            return _orig_mkdir(path, dir_fd=dir_fd)
        return _orig_mkdir(path)

    os.mkdir = _patched_mkdir  # type: ignore[assignment]
