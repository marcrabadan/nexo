//! crypto: secret, password, key, encrypt, sign (section 9, D97). The
//! algorithms are fixed: Argon2id (m=19456 KiB, t=2, p=1, a PHC string),
//! AES-256-GCM (a random 12-byte nonce before the ciphertext and its tag)
//! and Ed25519. Keys and signatures travel as base64 text.

use nexo_diag::{msg, Diagnostic};

use crate::state::{diag, failure, failure_as, R};
use crate::stdlib::{expects, missing_member};
use crate::value::*;

// ------------------------------------------------------------- base64

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn b64(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { B64[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { B64[n as usize & 63] as char } else { '=' });
    }
    out
}

/// Strict standard base64 (as Python's `b64decode(validate=True)`).
pub fn unb64(text: &str) -> Option<Vec<u8>> {
    let b = text.as_bytes();
    if b.len() % 4 != 0 {
        return None;
    }
    let val = |c: u8| B64.iter().position(|x| *x == c).map(|p| p as u32);
    let mut out = Vec::with_capacity(b.len() / 4 * 3);
    for (k, chunk) in b.chunks(4).enumerate() {
        let last = k == b.len() / 4 - 1;
        let pad = chunk.iter().rev().take_while(|c| **c == b'=').count();
        if pad > 2 || (pad > 0 && !last) {
            return None;
        }
        let mut n = 0u32;
        for (i, c) in chunk.iter().enumerate() {
            let v = if i >= 4 - pad { 0 } else { val(*c)? };
            n = (n << 6) | v;
        }
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Some(out)
}

pub fn random(n: usize) -> Vec<u8> {
    let mut buf = vec![0u8; n];
    getrandom::getrandom(&mut buf).expect("no source of random numbers");
    buf
}

// ------------------------------------------------------------ secrets

/// A value read from outside the program (the environment): it can never
/// be shown, turned into text or written (section 9).
pub struct Secret {
    pub name: String,
}

impl Secret {
    pub fn reveal(&self, span: u32) -> R<String> {
        match std::env::var(&self.name) {
            Ok(v) => Ok(v),
            Err(_) => Err(failure_as("NotFound", "NotFound.secret", span, &[("name", self.name.clone())])),
        }
    }
}

impl Native for Secret {
    fn type_name(&self) -> &str {
        "secret"
    }
    fn repr(&self) -> String {
        "secret".into()
    }
    fn is_secret(&self) -> bool {
        true
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

/// Where a secret may go (a key, a password): its text.
pub fn text_or_secret(fname: &str, v: V, span: u32) -> R<String> {
    if let Some(s) = v.native::<Secret>() {
        return s.reveal(span);
    }
    v.as_str().map(|s| s.to_string()).ok_or_else(|| expects(fname, "str, secret", v, span))
}

pub fn secret(name: V, from: Option<V>, span: u32) -> R<Val> {
    let n = name.as_str().ok_or_else(|| expects("secret", "str", name, span))?;
    if from.is_some_and(|f| !f.is_none()) {
        return Err(diag(Diagnostic::new_spanless("not_supported").arg("what", "\"secret(..., from: ...)\""), span));
    }
    Ok(native(Secret { name: n.to_string() }))
}

// ----------------------------------------------------------- passwords

pub fn password_hash(pw: V, span: u32) -> R<Val> {
    use argon2::password_hash::{PasswordHasher, SaltString};
    let text = text_or_secret("password.hash", pw, span)?;
    let salt = SaltString::encode_b64(&random(16)).unwrap();
    let hash = argon2::Argon2::default().hash_password(text.as_bytes(), &salt).unwrap();
    Ok(Val::str(hash.to_string()))
}

pub fn password_check(pw: V, stored: V, span: u32) -> R<Val> {
    use argon2::password_hash::{PasswordHash, PasswordVerifier};
    let text = text_or_secret("password.check", pw, span)?;
    let s = stored.as_str().ok_or_else(|| expects("password.check", "str", stored, span))?;
    let ok = match PasswordHash::new(s) {
        Ok(h) => h.algorithm.as_str() == "argon2id" && argon2::Argon2::default().verify_password(text.as_bytes(), &h).is_ok(),
        Err(_) => false,
    };
    Ok(Val::bool(ok))
}

const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

pub fn password_generate(length: Option<V>, span: u32) -> R<Val> {
    let n = match length {
        None => 20,
        Some(v) => match v.as_int() {
            Some(n) if n >= 1 => n as usize,
            _ => return Err(expects("password.generate", "int", v, span)),
        },
    };
    let mut out = String::with_capacity(n);
    while out.len() < n {
        for b in random(n) {
            // 62 * 4 = 248: no bias
            if (b as usize) < ALPHABET.len() * 4 && out.len() < n {
                out.push(ALPHABET[b as usize % ALPHABET.len()] as char);
            }
        }
    }
    Ok(Val::str(out))
}

// ---------------------------------------------------------------- keys

/// A key of AES-256-GCM.
pub struct Key {
    raw: Vec<u8>,
}

/// The private half of an Ed25519 key pair.
pub struct PrivateKey {
    raw: [u8; 32],
}

pub struct KeyPair {
    private: Val,
    public: String,
}

macro_rules! secret_value {
    ($t:ty, $name:expr) => {
        impl Native for $t {
            fn type_name(&self) -> &str {
                $name
            }
            fn repr(&self) -> String {
                $name.into()
            }
            fn is_secret(&self) -> bool {
                true
            }
            fn member(&self, me: V, name: &str, span: u32) -> R<Val> {
                self.field(me, name, span)
            }
            fn call_method(&self, _me: V, name: &str, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
                crate::stdlib::method_args(name, &[], 0, args, names, span)?;
                self.export(name, span)
            }
            fn as_any(&self) -> &dyn std::any::Any {
                self
            }
        }
    };
}

impl Key {
    fn field(&self, me: V, name: &str, span: u32) -> R<Val> {
        match name {
            "export" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "export"))),
            _ => Err(missing_member("key", name, &["export"], span)),
        }
    }
    fn export(&self, _name: &str, _span: u32) -> R<Val> {
        Ok(Val::str(b64(&self.raw)))
    }
}

impl PrivateKey {
    fn field(&self, me: V, name: &str, span: u32) -> R<Val> {
        match name {
            "export" => Ok(Val::new(Obj::BuiltinMethod(Val::from(me), "export"))),
            _ => Err(missing_member("private_key", name, &["export"], span)),
        }
    }
    fn export(&self, _name: &str, _span: u32) -> R<Val> {
        Ok(Val::str(b64(&self.raw)))
    }
}

impl KeyPair {
    fn field(&self, _me: V, name: &str, span: u32) -> R<Val> {
        match name {
            "private" => Ok(self.private.clone()),
            "public" => Ok(Val::str(self.public.clone())),
            _ => Err(missing_member("keypair", name, &["private", "public"], span)),
        }
    }
    fn export(&self, name: &str, span: u32) -> R<Val> {
        Err(missing_member("keypair", name, &["private", "public"], span))
    }
}

secret_value!(Key, "key");
secret_value!(PrivateKey, "private_key");
secret_value!(KeyPair, "keypair");

fn bad_key(what: &str, span: u32) -> crate::state::Fail {
    failure("InvalidValue", span, &[("detail", msg("detail.bad_key", &[("what", what.to_string())]))])
}

pub fn key_new() -> R<Val> {
    Ok(native(Key { raw: random(32) }))
}

pub fn key_load(text: V, span: u32) -> R<Val> {
    let t = text_or_secret("key.load", text, span)?;
    match unb64(&t) {
        Some(raw) if raw.len() == 32 => Ok(native(Key { raw })),
        _ => Err(bad_key("AES-256", span)),
    }
}

fn pair_of(raw: [u8; 32]) -> Val {
    let sk = ed25519_dalek::SigningKey::from_bytes(&raw);
    let public = b64(&sk.verifying_key().to_bytes());
    native(KeyPair { private: native(PrivateKey { raw }), public })
}

pub fn keypair_new() -> R<Val> {
    let raw: [u8; 32] = random(32).try_into().unwrap();
    Ok(pair_of(raw))
}

pub fn keypair_load(text: V, span: u32) -> R<Val> {
    let t = text_or_secret("keypair.load", text, span)?;
    match unb64(&t).and_then(|r| <[u8; 32]>::try_from(r).ok()) {
        Some(raw) => Ok(pair_of(raw)),
        None => Err(bad_key("Ed25519", span)),
    }
}

fn the_key<'a>(fname: &str, v: V, span: u32) -> R<&'a [u8]> {
    match v.native::<Key>() {
        Some(k) => Ok(&k.raw),
        None => Err(expects(fname, "key", v, span)),
    }
}

fn seal(key: &[u8], data: &[u8]) -> Vec<u8> {
    use aes_gcm::aead::{Aead, KeyInit};
    let cipher = aes_gcm::Aes256Gcm::new_from_slice(key).unwrap();
    let nonce = random(12);
    let mut out = nonce.clone();
    out.extend(cipher.encrypt(aes_gcm::Nonce::from_slice(&nonce), data).unwrap());
    out
}

fn not_encrypted(span: u32) -> crate::state::Fail {
    failure("InvalidData", span, &[("detail", msg("detail.not_encrypted", &[]))])
}

fn unseal(key: &[u8], blob: Option<Vec<u8>>, span: u32) -> R<Vec<u8>> {
    use aes_gcm::aead::{Aead, KeyInit};
    let Some(blob) = blob.filter(|b| b.len() >= 28) else { return Err(not_encrypted(span)) };
    let cipher = aes_gcm::Aes256Gcm::new_from_slice(key).unwrap();
    cipher.decrypt(aes_gcm::Nonce::from_slice(&blob[..12]), &blob[12..]).map_err(|_| not_encrypted(span))
}

pub fn encrypt(data: V, key: V, span: u32) -> R<Val> {
    let k = the_key("encrypt", key, span)?;
    let d = data.as_str().ok_or_else(|| expects("encrypt", "str", data, span))?;
    Ok(Val::str(b64(&seal(k, d.as_bytes()))))
}

pub fn decrypt(data: V, key: V, span: u32) -> R<Val> {
    let k = the_key("decrypt", key, span)?;
    let d = data.as_str().ok_or_else(|| expects("decrypt", "str", data, span))?;
    let plain = unseal(k, unb64(d), span)?;
    String::from_utf8(plain).map(Val::str).map_err(|_| not_encrypted(span))
}

fn file_bytes(path: &str, span: u32) -> R<Vec<u8>> {
    crate::repl::read_file(|| std::fs::read(path)).map_err(|e| crate::data::os_failure(path, &e, span))
}

fn put_bytes(path: &str, data: &[u8], span: u32) -> R<()> {
    crate::repl::write_file(|| std::fs::write(path, data)).map_err(|e| crate::data::os_failure(path, &e, span))
}

pub fn encrypt_file(path: V, key: V, span: u32) -> R<Val> {
    let p = crate::stdlib::str_arg("encrypt_file", &path, span)?;
    let k = the_key("encrypt_file", key, span)?;
    let data = file_bytes(p, span)?;
    put_bytes(p, &seal(k, &data), span)?;
    Ok(Val::none())
}

pub fn decrypt_file(path: V, key: V, span: u32) -> R<Val> {
    let p = crate::stdlib::str_arg("decrypt_file", &path, span)?;
    let k = the_key("decrypt_file", key, span)?;
    let data = file_bytes(p, span)?;
    let plain = unseal(k, Some(data), span)?;
    put_bytes(p, &plain, span)?;
    Ok(Val::none())
}

pub fn sign(document: V, private: V, span: u32) -> R<Val> {
    use ed25519_dalek::Signer;
    let Some(k) = private.native::<PrivateKey>() else { return Err(expects("sign", "private_key", private, span)) };
    let d = document.as_str().ok_or_else(|| expects("sign", "str", document, span))?;
    let sk = ed25519_dalek::SigningKey::from_bytes(&k.raw);
    Ok(Val::str(b64(&sk.sign(d.as_bytes()).to_bytes())))
}

pub fn verify(document: V, signature: V, public: V, span: u32) -> R<Val> {
    for v in [document, signature, public] {
        if v.as_str().is_none() {
            return Err(expects("verify", "str", v, span));
        }
    }
    let sig = unb64(signature.as_str().unwrap()).and_then(|s| <[u8; 64]>::try_from(s).ok());
    let pk = unb64(public.as_str().unwrap()).and_then(|s| <[u8; 32]>::try_from(s).ok());
    let (Some(sig), Some(pk)) = (sig, pk) else { return Ok(Val::bool(false)) };
    let Ok(vk) = ed25519_dalek::VerifyingKey::from_bytes(&pk) else { return Ok(Val::bool(false)) };
    let sig = ed25519_dalek::Signature::from_bytes(&sig);
    Ok(Val::bool(vk.verify_strict(document.as_str().unwrap().as_bytes(), &sig).is_ok()))
}
