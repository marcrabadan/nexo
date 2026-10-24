"""crypto: secret, password, key, encrypt, sign (section 9, D97).

Algorithms are fixed: Argon2id for passwords (m=19456 KiB, t=2, p=1, as a
PHC string), AES-256-GCM for encryption (a random 12-byte nonce before the
ciphertext and its tag, in base64 for texts) and Ed25519 for signatures.
Keys and signatures travel as base64 text.
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets as _secrets

from .. import errors
from ..errors import NexoError
from ..types import Builtin, Native, failure
from . import Namespace


class Secret(Native):
    """A value read from outside the program (the environment) that can
    never be shown, turned into text or written (section 9)."""
    type_name = "secret"
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def repr(self) -> str:
        raise NexoError("secret_shown", None)

    def reveal(self, call) -> str:
        value = os.environ.get(self.name)
        if value is None:
            raise failure("NotFound", call.span, key="NotFound.secret", name=self.name)
        return value


def text_or_secret(call, fn: str, value) -> str:
    """Where a secret may go (a key, a password to a database): its text."""
    if isinstance(value, Secret):
        return value.reveal(call)
    if isinstance(value, str):
        return value
    from .core import expects
    raise expects(call, fn, "str, secret", value)


def b_secret(call, name, from_=None):
    if not isinstance(name, str):
        from .core import expects
        raise expects(call, "secret", "str", name)
    if from_ is not None:
        raise NexoError("not_supported", call.span, what='"secret(..., from: ...)"')
    return Secret(name)


# -------------------------------------------------------------- password

ARGON = dict(length=32, iterations=2, lanes=1, memory_cost=19456)


def p_hash(call, password):
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    text = text_or_secret(call, "password.hash", password)
    return Argon2id(salt=os.urandom(16), **ARGON).derive_phc_encoded(text.encode("utf-8"))


def p_check(call, password, stored):
    from cryptography.exceptions import InvalidKey
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    text = text_or_secret(call, "password.check", password)
    if not isinstance(stored, str):
        from .core import expects
        raise expects(call, "password.check", "str", stored)
    try:
        Argon2id.verify_phc_encoded(text.encode("utf-8"), stored)
        return True
    except (InvalidKey, ValueError, TypeError):
        return False


ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def p_generate(call, length=20):
    if not isinstance(length, int) or isinstance(length, bool) or length < 1:
        from .core import expects
        raise expects(call, "password.generate", "int", length)
    return "".join(_secrets.choice(ALPHABET) for _ in range(length))


# ------------------------------------------------------------------- keys

def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None


class Key(Native):
    """A key of AES-256-GCM."""
    type_name = "key"
    __slots__ = ("raw",)

    def __init__(self, raw: bytes):
        self.raw = raw

    def repr(self) -> str:
        raise NexoError("secret_shown", None)

    def member(self, call, name):
        if name == "export":
            return Builtin("export", lambda c: b64(self.raw))
        return self.missing(call, name, ["export"])


class PrivateKey(Native):
    """The private half of an Ed25519 key pair."""
    type_name = "private_key"
    __slots__ = ("raw",)

    def __init__(self, raw: bytes):
        self.raw = raw

    def repr(self) -> str:
        raise NexoError("secret_shown", None)

    def member(self, call, name):
        if name == "export":
            return Builtin("export", lambda c: b64(self.raw))
        return self.missing(call, name, ["export"])


class KeyPair(Native):
    type_name = "keypair"
    __slots__ = ("private", "public")

    def __init__(self, private: PrivateKey, public: str):
        self.private, self.public = private, public

    def repr(self) -> str:
        raise NexoError("secret_shown", None)

    def member(self, call, name):
        if name == "private":
            return self.private
        if name == "public":
            return self.public
        return self.missing(call, name, ["private", "public"])


def invalid_key(call, what: str):
    return failure("InvalidValue", call.span, detail=errors.msg("detail.bad_key", what=what))


def k_new(call):
    return Key(os.urandom(32))


def k_load(call, text):
    raw = unb64(text_or_secret(call, "key.load", text))
    if raw is None or len(raw) != 32:
        raise invalid_key(call, "AES-256")
    return Key(raw)


def pair_of(raw: bytes) -> KeyPair:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pub = Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return KeyPair(PrivateKey(raw), b64(pub))


def kp_new(call):
    return pair_of(os.urandom(32))


def kp_load(call, text):
    raw = unb64(text_or_secret(call, "keypair.load", text))
    if raw is None or len(raw) != 32:
        raise invalid_key(call, "Ed25519")
    return pair_of(raw)


def need(call, fn: str, value, cls, what: str):
    if not isinstance(value, cls):
        from .core import expects
        raise expects(call, fn, what, value)
    return value


def seal(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def unseal(call, key: bytes, blob: bytes | None) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob is None or len(blob) < 28:
        raise failure("InvalidData", call.span, detail=errors.msg("detail.not_encrypted"))
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], None)
    except InvalidTag:
        raise failure("InvalidData", call.span, detail=errors.msg("detail.not_encrypted"))


def b_encrypt(call, data, key):
    k = need(call, "encrypt", key, Key, "key")
    if not isinstance(data, str):
        from .core import expects
        raise expects(call, "encrypt", "str", data)
    return b64(seal(k.raw, data.encode("utf-8")))


def b_decrypt(call, data, key):
    k = need(call, "decrypt", key, Key, "key")
    if not isinstance(data, str):
        from .core import expects
        raise expects(call, "decrypt", "str", data)
    plain = unseal(call, k.raw, unb64(data))
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise failure("InvalidData", call.span, detail=errors.msg("detail.not_encrypted"))


def file_bytes(call, path) -> bytes:
    from .io import check_path, os_failure
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        raise os_failure(call, path, exc)


def put_bytes(call, path, data: bytes) -> None:
    from .io import os_failure
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as exc:
        raise os_failure(call, path, exc)


def b_encrypt_file(call, path, key):
    from .io import check_path
    path = check_path(call, path, "encrypt_file")
    k = need(call, "encrypt_file", key, Key, "key")
    put_bytes(call, path, seal(k.raw, file_bytes(call, path)))
    return None


def b_decrypt_file(call, path, key):
    from .io import check_path
    path = check_path(call, path, "decrypt_file")
    k = need(call, "decrypt_file", key, Key, "key")
    put_bytes(call, path, unseal(call, k.raw, file_bytes(call, path)))
    return None


def b_sign(call, document, private):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = need(call, "sign", private, PrivateKey, "private_key")
    if not isinstance(document, str):
        from .core import expects
        raise expects(call, "sign", "str", document)
    return b64(Ed25519PrivateKey.from_private_bytes(k.raw).sign(document.encode("utf-8")))


def b_verify(call, document, signature, public):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from .core import expects
    for v in (document, signature, public):
        if not isinstance(v, str):
            raise expects(call, "verify", "str", v)
    sig, pub = unb64(signature), unb64(public)
    if sig is None or pub is None or len(pub) != 32 or len(sig) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, document.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError):
        return False


def exports() -> dict:
    return {
        "secret": Builtin("secret", b_secret),
        "password": Namespace("password", {
            "hash": Builtin("password.hash", p_hash),
            "check": Builtin("password.check", p_check),
            "generate": Builtin("password.generate", p_generate),
        }),
        "key": Namespace("key", {"new": Builtin("key.new", k_new), "load": Builtin("key.load", k_load)}),
        "keypair": Namespace("keypair", {"new": Builtin("keypair.new", kp_new), "load": Builtin("keypair.load", kp_load)}),
        "encrypt": Builtin("encrypt", b_encrypt),
        "decrypt": Builtin("decrypt", b_decrypt, fallible=True),
        "encrypt_file": Builtin("encrypt_file", b_encrypt_file, fallible=True),
        "decrypt_file": Builtin("decrypt_file", b_decrypt_file, fallible=True),
        "sign": Builtin("sign", b_sign),
        "verify": Builtin("verify", b_verify),
    }
