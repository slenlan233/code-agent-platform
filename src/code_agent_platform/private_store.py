"""Local service secrets; Windows credentials are encrypted for the current user."""
import base64
import ctypes
import json
import os
import tempfile
from pathlib import Path


def _dpapi(data,decrypt=False):
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_=[('length',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    source=Blob(len(data),buffer); target=Blob()
    function=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)):
        raise OSError('local_secret_unavailable')
    try:return ctypes.string_at(target.data,target.length)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes=[ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data,ctypes.c_void_p))


class PrivateStore:
    def __init__(self,root):
        self.root=Path(root)/'secrets'
        self.root.mkdir(parents=True,exist_ok=True)
        self.root.chmod(0o700)

    def path(self,name):
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',name):raise ValueError('secret_name')
        return self.root/(name+'.json')

    def read(self,name):
        path=self.path(name)
        if not path.exists():return None
        saved=json.loads(path.read_text(encoding='utf-8'))
        raw=base64.b64decode(saved['payload'])
        if saved['protection']=='windows-dpapi':raw=_dpapi(raw,True)
        elif saved['protection']!='file-permissions':raise ValueError('secret_protection')
        return json.loads(raw)

    def delete(self, name):
        self.path(name).unlink(missing_ok=True)

    def write(self,name,value):
        raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
        protection='windows-dpapi' if os.name=='nt' else 'file-permissions'
        if os.name=='nt':raw=_dpapi(raw)
        data=json.dumps({'protection':protection,'payload':base64.b64encode(raw).decode()})
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.root,delete=False) as handle:
            temporary=Path(handle.name)
            handle.write(data)
        try:
            temporary.chmod(0o600)
            os.replace(temporary,self.path(name))
            self.path(name).chmod(0o600)
        finally:temporary.unlink(missing_ok=True)
