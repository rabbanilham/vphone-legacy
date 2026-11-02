"""Copy app content without host metadata, resource forks or unsafe links."""
from pathlib import Path
import os
import shutil
import stat
import subprocess
import sys


def copy_tree_clean(src, dst):
    """Copy a tree into a new destination, preserving permission bits only.

    Uses fresh files and copyfile (never copy2/copystat), equivalent to ditto
    --noextattr --norsrc with Finder sidecars excluded. Rejects symbolic links
    and special files in both trees; the destination must not exist.
    """
    src, dst = Path(src), Path(dst)
    def scan(path):
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f'Cannot copy symlink or special file: {path}')
        if stat.S_ISDIR(mode):
            for child in path.iterdir():
                if child.name != '.DS_Store' and not child.name.startswith('._'):
                    scan(child)
    scan(src)
    if not src.is_dir():
        raise ValueError('Source must be a directory')
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(dst)
    def copy(source, target):
        mode = stat.S_IMODE(source.stat().st_mode)
        if source.is_dir():
            target.mkdir(mode=0o700)
            for child in source.iterdir():
                if child.name != '.DS_Store' and not child.name.startswith('._'):
                    copy(child, target / child.name)
        else:
            shutil.copyfile(source, target)
        # Fresh objects avoid source xattrs; remove inherited destination xattrs too.
        if sys.platform == 'darwin':
            subprocess.run(['xattr', '-c', str(target)], check=True, capture_output=True)
        else:
            for name in os.listxattr(target):
                os.removexattr(target, name)
        target.chmod(mode)
    copy(src, dst)
    return dst
