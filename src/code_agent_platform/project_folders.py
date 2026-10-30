"""Browse configured roots, or issue narrowly scoped local folder selections."""
from pathlib import Path
import stat
import os
import re

from .models import PlatformError
from .security import safe_path


def local_roots():
    if os.name != "nt":
        return [Path("/")]
    import ctypes
    return [Path(value) for value in os.listdrives() if ctypes.windll.kernel32.GetDriveTypeW(value) in {2,3,6}]


def selected_directory(value, *, allow_volume=False):
    requested = Path(value)
    if not requested.is_absolute() or ".." in requested.parts or str(requested).startswith("\\\\"):
        raise PlatformError("folder_invalid", "请选择本机文件夹的绝对路径。", 403)
    if not any(requested.is_relative_to(root) for root in local_roots()):
        raise PlatformError("folder_scope", "只支持本机磁盘上的项目文件夹。", 403)
    try:
        for component in (requested,*requested.parents):
            info=component.lstat()
            if component.is_symlink() or getattr(info,"st_file_attributes",0) & getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0x400):
                raise PlatformError("link_denied", "不允许通过链接选择项目文件夹。", 403)
        canonical=requested.resolve()
        root=Path(canonical.anchor)
        current=safe_path(root,canonical.relative_to(root).as_posix())
        if not current.is_dir():
            raise PlatformError("folder_invalid", "请选择已存在的文件夹。")
        if not allow_volume and current==root:
            raise PlatformError("folder_volume", "请选择具体项目文件夹，不能把整块磁盘登记为项目。", 403)
        return current
    except OSError:
        raise PlatformError("folder_access", "无法访问这个本机文件夹。", 403) from None


def directory_identity(root):
    info=root.stat()
    return {"device":info.st_dev,"inode":info.st_ino}


def create_folder(parent,name):
    if name!=name.strip() or not re.fullmatch(r'[^\\/:*?"<>|\x00-\x1f.][^\\/:*?"<>|\x00-\x1f]*',name) or name.endswith(("."," ")) or name.split(".")[0].upper() in {"CON","PRN","AUX","NUL",*(f"COM{i}" for i in range(10)),*(f"LPT{i}" for i in range(10))}:
        raise PlatformError("folder_name","请输入普通文件夹名称，不含路径或特殊字符。")
    base=selected_directory(parent,allow_volume=True)
    target=safe_path(base,name,must_exist=False)
    try:target.mkdir(exist_ok=False)
    except FileExistsError:raise PlatformError("folder_exists","这个文件夹已经存在，请选择它或使用其他名称。",409) from None
    except OSError:raise PlatformError("folder_access","无法在这个位置创建项目文件夹。",403) from None
    return selected_directory(str(target))


def browse_folders(settings, value=None, *, local=False):
    if local and not settings.allow_local_project_selection:
        raise PlatformError("folder_selection_disabled", "本机项目选择尚未启用。", 403)
    if local:
        volumes=local_roots()
        if value is None:
            return {"path":None,"parent":None,"local_selection":True,"selectable":False,"allowed_roots":[str(item.resolve()) for item in settings.allowed_roots],
                    "folders":[{"name":str(item),"path":str(item)} for item in volumes if item.is_dir()]}
        current=selected_directory(value,allow_volume=True)
        root=Path(current.anchor)
    else:
        return _browse_configured(settings,value)
    return _list_folders(current,root,settings,local=True)


def _browse_configured(settings,value):
    roots = [item.resolve() for item in settings.allowed_roots]
    if value is None:
        return {"path":None, "parent":None, "allowed_roots":[str(item) for item in roots],
                "folders":[{"name":item.name or str(item),"path":str(item)} for item in roots if item.is_dir()]}
    requested = Path(value)
    if not requested.is_absolute():
        raise PlatformError("folder_invalid", "请输入绝对文件夹路径。")
    if ".." in requested.parts:
        raise PlatformError("folder_scope", "文件夹路径不能包含上级跳转。", 403)
    canonical = requested.resolve()
    root = next((item for item in roots if canonical.is_relative_to(item)), None)
    if root is None:
        raise PlatformError("folder_scope", "这个文件夹不在当前允许范围内，请先由管理员登记允许目录。", 403)
    # Canonicalization handles Windows short names; reject actual links before
    # using the canonical spelling, so aliases cannot bypass link restrictions.
    for component in (requested,*requested.parents):
        info = component.lstat()
        if component.is_symlink() or getattr(info,"st_file_attributes",0) & getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0x400):
            raise PlatformError("link_denied", "不允许通过链接选择项目文件夹。", 403)
    current = safe_path(root, canonical.relative_to(root).as_posix())
    if not current.is_dir():
        raise PlatformError("folder_invalid", "请选择已存在的文件夹。")
    return _list_folders(current,root,settings)


def _list_folders(current,root,settings,*,local=False):
    folders = []
    try:
        for child in current.iterdir():
            try:
                candidate = safe_path(root,child.relative_to(root).as_posix())
                if candidate.is_dir():
                    folders.append({"name":candidate.name,"path":str(candidate)})
            except (OSError,PlatformError):
                continue
            if len(folders)>=300:
                break
    except PermissionError:
        raise PlatformError("folder_access", "当前账号无法打开这个文件夹。", 403) from None
    return {"path":str(current),"parent":str(current.parent) if current!=root else None,"local_selection":local,"selectable":not local or current!=root,
            "allowed_roots":[str(item.resolve()) for item in settings.allowed_roots],"folders":sorted(folders,key=lambda item:item["name"].lower()),"limited":len(folders)>=300}
