from pathlib import Path
import ctypes, hashlib, json, os, re, sys, tempfile
from ctypes import wintypes

class _Blob(ctypes.Structure):
    _fields_=[('cbData',ctypes.c_uint32),('pbData',ctypes.POINTER(ctypes.c_ubyte))]

_CRYPT32 = ctypes.WinDLL('crypt32.dll', use_last_error=True) if sys.platform == 'win32' else None
_KERNEL32 = ctypes.WinDLL('kernel32.dll', use_last_error=True) if sys.platform == 'win32' else None
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
if _CRYPT32 is not None:
    _CRYPT32.CryptProtectData.argtypes=[ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _CRYPT32.CryptProtectData.restype=wintypes.BOOL
    _CRYPT32.CryptUnprotectData.argtypes=[ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _CRYPT32.CryptUnprotectData.restype=wintypes.BOOL
    _KERNEL32.LocalFree.argtypes=[wintypes.HLOCAL]
    _KERNEL32.LocalFree.restype=wintypes.HLOCAL

class CredentialStore:
    """Windows DPAPI Current User store; never uses machine scope."""
    def __init__(self,state_dir=None):
        configured = state_dir or os.environ.get('ILINK_STATE_DIR')
        self.state_dir=Path(configured) if configured else Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ilink-receive-probe'; self.path=self.state_dir/'credential.dat'; self.meta=self.state_dir/'credential.meta.json'
    def _protect(self,data,unprotect=False):
        if sys.platform!='win32': raise RuntimeError('Windows DPAPI is required')
        srcbuf=(ctypes.c_ubyte*len(data)).from_buffer_copy(data); src=_Blob(len(data),srcbuf); dst=_Blob()
        description=wintypes.LPWSTR()
        if unprotect:
            ok=_CRYPT32.CryptUnprotectData(ctypes.byref(src),ctypes.byref(description),None,None,None,_CRYPTPROTECT_UI_FORBIDDEN,ctypes.byref(dst))
        else:
            ok=_CRYPT32.CryptProtectData(ctypes.byref(src),None,None,None,None,_CRYPTPROTECT_UI_FORBIDDEN,ctypes.byref(dst))
        if not ok:
            error=ctypes.get_last_error()
            raise RuntimeError(f'DPAPI operation failed: WinError {error}')
        try: return ctypes.string_at(dst.pbData,dst.cbData)
        finally:
            if dst.pbData: _KERNEL32.LocalFree(ctypes.cast(dst.pbData,wintypes.HLOCAL))
            if description: _KERNEL32.LocalFree(ctypes.cast(description,wintypes.HLOCAL))
    @staticmethod
    def fingerprint(secret):
        """Return the bounded fingerprint used to bind metadata to a token."""
        if not isinstance(secret, str) or not secret:
            raise ValueError('credential secret must be a non-empty string')
        return hashlib.sha256(secret.encode('utf-8')).hexdigest()[:12]

    @staticmethod
    def _is_fingerprint(value):
        return isinstance(value, str) and bool(
            re.fullmatch(r'[0-9a-fA-F]{8,64}', value)
        )

    def _safe_existing_metadata(self):
        """Read only the redacted metadata fields that this store owns."""
        try:
            value = json.loads(self.meta.read_text(encoding='utf-8'))
        except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        allowed = {
            'account_fingerprint', 'bot_fingerprint', 'credential_fingerprint',
            'state',
        }
        return {
            key: value[key]
            for key in allowed
            if key in value and (
                key == 'state'
                and isinstance(value[key], str)
                and re.fullmatch(r'[A-Za-z0-9_-]{1,32}', value[key])
                or key != 'state'
                and self._is_fingerprint(value[key])
            )
        }

    def _write_metadata(self, metadata):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(metadata, ensure_ascii=True, separators=(',', ':'))
        fd, temporary = tempfile.mkstemp(
            prefix=self.meta.name + '.', suffix='.tmp', dir=self.state_dir,
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.meta)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def save(self, secret, *, account_fingerprint=None, bot_fingerprint=None):
        """Save a token with only bounded identity metadata.

        The token is encrypted with Windows DPAPI Current User.  The metadata
        file never contains the token (or any raw protocol identity); it only
        carries short fingerprints.  Optional account/bot fingerprints are
        used by the QR-login path, while ordinary callers remain compatible
        with the historical ``save(secret)`` API.
        """
        credential_fingerprint = self.fingerprint(secret)
        if account_fingerprint is not None and not self._is_fingerprint(account_fingerprint):
            raise ValueError('account_fingerprint must be a bounded fingerprint')
        if bot_fingerprint is not None and not self._is_fingerprint(bot_fingerprint):
            raise ValueError('bot_fingerprint must be a bounded fingerprint')
        encrypted = self._protect(secret.encode('utf-8'))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(encrypted)
        metadata = self._safe_existing_metadata()
        metadata.update({
            'schema_version': 1,
            'credential_type': 'DPAPI_CURRENT_USER',
            'credential_fingerprint': credential_fingerprint,
        })
        if account_fingerprint is not None:
            metadata['account_fingerprint'] = account_fingerprint.lower()
        else:
            metadata.pop('account_fingerprint', None)
        if bot_fingerprint is not None:
            metadata['bot_fingerprint'] = bot_fingerprint.lower()
        else:
            metadata.pop('bot_fingerprint', None)
        self._write_metadata(metadata)
    def load(self): return self._protect(self.path.read_bytes(),True).decode('utf-8')
    def delete(self):
        for p in (self.path,self.meta):
            try:p.unlink()
            except FileNotFoundError:pass
    def describe_location(self): return {'directory':str(self.state_dir),'file':str(self.path),'metadata':str(self.meta),'plaintext':False,'scope':'CURRENT_USER'}
    def __repr__(self): return 'CredentialStore(secret=<redacted>)'
