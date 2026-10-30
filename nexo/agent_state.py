"""Operator-granted encrypted checkpoints, separate from application file authority."""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import os
import secrets
import stat
import sys

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

@dataclass(frozen=True)
class Grant:
    directory: Path
    device: int
    inode: int
    key: str
    max_bytes: int
    budget: object

def parse(value, base):
    if os.name != 'posix' or sys.platform in ('emscripten', 'wasi'):
        raise ValueError('unsupported state host')
    if not isinstance(value, dict) or set(value) != {'directory', 'key', 'max_bytes', 'budget'}:
        raise ValueError('invalid state grant')
    key, size = (value['key'], value['max_bytes'])
    if not isinstance(key, str) or not key.isascii() or (not key) or (not all((c.isalnum() or c == '_' for c in key))):
        raise ValueError('invalid state key name')
    if type(size) is not int or not 4096 <= size <= 16777216 or (not isinstance(value['directory'], str)):
        raise ValueError('invalid state bound')
    path = base / value['directory']
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 448:
        raise ValueError('state directory must be private and owned')
    budget = value['budget']
    if budget is not None:
        if not isinstance(budget, dict) or set(budget) != {'limit', 'currency', 'period', 'prices'}:
            raise ValueError('invalid state budget')
        if budget['period'] not in ('day', 'month') or not isinstance(budget['currency'], str) or len(budget['currency']) != 3 or (not budget['currency'].isascii()) or (not budget['currency'].isalpha()) or (not budget['currency'].isupper()):
            raise ValueError('invalid state budget period/currency')

        def amount(v, positive=False):
            if not isinstance(v, str) or len(v) > 32 or (not v) or any((c not in '0123456789.' for c in v)) or (v.count('.') > 1) or (len(v.partition('.')[2]) > 6) or (not any((c.isdigit() for c in v))):
                raise ValueError('invalid state price')
            n = Decimal(v)
            if not n.is_finite() or n < 0 or n > Decimal('1000000000000') or (positive and (not n)):
                raise ValueError('invalid state amount')
        amount(budget['limit'], True)
        if not isinstance(budget['prices'], dict) or not budget['prices']:
            raise ValueError('missing state prices')
        for model, prices in budget['prices'].items():
            if not isinstance(model, str) or ':' not in model or (not isinstance(prices, list)) or (len(prices) != 2):
                raise ValueError('invalid state prices')
            for price in prices:
                amount(price)
    return Grant(path.resolve(strict=True), info.st_dev, info.st_ino, key, size, budget)

class Store:

    def __init__(self, grant):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.grant = grant
        text = os.environ.get(grant.key, '')
        if len(text) != 64 or any((c not in '0123456789abcdefABCDEF' for c in text)):
            raise ValueError('invalid state key')
        self.cipher = AESGCM(bytes.fromhex(text))
        self.fd = os.open(grant.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(self.fd)
            if (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)) != (grant.device, grant.inode, os.geteuid(), 448):
                raise ValueError('state directory changed')
        except BaseException:
            self.close()
            raise

    def __del__(self):
        if getattr(self, 'fd', None) is not None:
            self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def valid(self, fd):
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.geteuid() or (stat.S_IMODE(s.st_mode) != 384) or (s.st_size > self.grant.max_bytes):
            raise ValueError('invalid state file')

    @contextmanager
    def lock(self, name):
        import fcntl
        fd = os.open(name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 384, dir_fd=self.fd)
        try:
            self.valid(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def read(self, name):
        try:
            fd = os.open(name + '.state', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        try:
            self.valid(fd)
            with os.fdopen(fd, 'rb', closefd=False) as f:
                data = f.read(self.grant.max_bytes + 1)
            if len(data) > self.grant.max_bytes or len(data) < 28:
                raise ValueError('invalid state size')
            text = self.cipher.decrypt(data[:12], data[12:], name.encode())
            from .json_limits import nesting_within
            if not nesting_within(text):
                raise ValueError('invalid state depth')

            def unique(pairs):
                obj = {}
                for k, v in pairs:
                    if k in obj:
                        raise ValueError('duplicate state key')
                    obj[k] = v
                return obj
            return json.loads(text, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite state')))
        finally:
            os.close(fd)

    def write(self, name, value):
        text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        if len(text) + 28 > self.grant.max_bytes:
            raise ValueError('state exceeds grant')
        nonce = secrets.token_bytes(12)
        data = nonce + self.cipher.encrypt(nonce, text, name.encode())
        fresh = secrets.token_hex(16) + '.tmp'
        fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 384, dir_fd=self.fd)
        try:
            from .checkpoints import commit

            def write_and_sync():
                with os.fdopen(fd, 'wb', closefd=False) as f:
                    f.write(data)
                    f.flush()
                    os.fsync(fd)
            commit(write_and_sync, lambda: os.rename(fresh, name + '.state', src_dir_fd=self.fd, dst_dir_fd=self.fd), lambda: os.fsync(self.fd))
        finally:
            os.close(fd)
            try:
                os.unlink(fresh, dir_fd=self.fd)
            except FileNotFoundError:
                pass
