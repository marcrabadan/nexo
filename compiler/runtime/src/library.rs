//! Builtins of the standard library added in phase 4 (section 15) and the
//! internal functions module blocks are rewritten into (D90). Their ids
//! start at BASE; `stdlib::call` sends them here. A name with a dot
//! (`password.hash`) is a member of a namespace.

use crate::state::R;
use crate::stdlib::{check_args, opt, p, Sig};
use crate::value::*;

pub const BASE: u32 = 100;

static SIGS: &[Sig] = &[
    Sig { name: "move_file", params: &[p("path"), p("to")] },
    Sig { name: "regex", params: &[p("pattern")] },
    Sig { name: "render", params: &[p("path")] },
    Sig { name: "secret", params: &[p("name"), opt("from")] },
    Sig { name: "password.hash", params: &[p("password")] },
    Sig { name: "password.check", params: &[p("password"), p("stored")] },
    Sig { name: "password.generate", params: &[opt("length")] },
    Sig { name: "key.new", params: &[] },
    Sig { name: "key.load", params: &[p("text")] },
    Sig { name: "keypair.new", params: &[] },
    Sig { name: "keypair.load", params: &[p("text")] },
    Sig { name: "encrypt", params: &[p("data"), p("key")] },
    Sig { name: "decrypt", params: &[p("data"), p("key")] },
    Sig { name: "encrypt_file", params: &[p("path"), p("key")] },
    Sig { name: "decrypt_file", params: &[p("path"), p("key")] },
    Sig { name: "sign", params: &[p("document"), p("private")] },
    Sig { name: "verify", params: &[p("document"), p("signature"), p("public")] },
    Sig { name: "http.get", params: &[] },
    Sig { name: "http.post", params: &[] },
    Sig { name: "http.put", params: &[] },
    Sig { name: "http.patch", params: &[] },
    Sig { name: "http.delete", params: &[] },
    Sig { name: "__fake", params: &[p("name"), p("value")] },
    Sig { name: "connect", params: &[p("url"), opt("key")] },
    Sig { name: "__db_table", params: &[p("type"), p("key"), p("many"), p("marks")] },
    Sig { name: "__db_begin", params: &[p("db")] },
    Sig { name: "__db_commit", params: &[p("tx")] },
    Sig { name: "__db_rollback", params: &[p("tx")] },
    Sig { name: "__sql_text", params: &[p("parts"), p("values")] },
    Sig { name: "jwt", params: &[p("key")] },
    Sig { name: "log", params: &[] },
    Sig { name: "ask", params: &[] },
    Sig { name: "__ai_config", params: &[] },
    Sig { name: "__ai_models", params: &[] },
    Sig { name: "__ai_tool", params: &[p("fn"), p("description"), p("types")] },
    Sig { name: "__ai_agent", params: &[] },
    Sig { name: "mcp", params: &[p("name")] },
    Sig { name: "__c_library", params: &[p("name"), p("bound"), opt("info")] },
    Sig { name: "__python_import", params: &[p("name"), opt("info")] },
    Sig { name: "__rate", params: &[] },
    Sig { name: "__web_server", params: &[p("name"), opt("port"), opt("auth")] },
    Sig {
        name: "__web_route",
        params: &[p("server"), p("method"), p("path"), p("handler"), p("types"), opt("role"), opt("limit"), opt("public")],
    },
];

/// Namespaces of the library: their members are the SIGS named "ns.member".
const NAMESPACES: &[&str] = &["password", "key", "keypair", "http"];

fn id_of(name: &str) -> Option<u32> {
    SIGS.iter().position(|s| s.name == name).map(|i| BASE + i as u32)
}

pub fn name(id: BuiltinId) -> &'static str {
    SIGS[(id.0 - BASE) as usize].name
}

/// Builtin names of the library as values.
pub fn value(name: &str) -> Option<Val> {
    if let Some(ns) = NAMESPACES.iter().find(|n| **n == name) {
        return Some(Val::new(Obj::Namespace(ns)));
    }
    if name.contains('.') {
        return None;
    }
    Some(Val::new(Obj::Builtin(BuiltinId(id_of(name)?))))
}

/// Members of the library's namespaces (`password.hash`...).
pub fn member(ns: &str, name: &str, span: u32) -> Option<R<Val>> {
    if !NAMESPACES.contains(&ns) {
        return None;
    }
    let full = format!("{ns}.{name}");
    Some(match id_of(&full) {
        Some(id) => Ok(Val::new(Obj::Builtin(BuiltinId(id)))),
        None => {
            let prefix = format!("{ns}.");
            let names: Vec<&str> = SIGS.iter().filter_map(|s| s.name.strip_prefix(&prefix)).collect();
            Err(crate::stdlib::missing_member(ns, name, &names, span))
        }
    })
}

static FAKES: std::sync::Mutex<Vec<(String, Val)>> = std::sync::Mutex::new(Vec::new());

/// The value a `fake` of the running test gives calls of `name`.
pub fn fake_value(name: &str) -> Option<Val> {
    FAKES.lock().unwrap().iter().rev().find(|(n, _)| n == name).map(|(_, v)| v.clone())
}

/// End of the program (NEXO_LEAKCHECK): what the library still holds.
pub fn release_all() {
    clear_fakes();
    crate::db::release_all();
    crate::web::release_all();
    crate::ai::release_all();
}

pub fn clear_fakes() {
    let old = std::mem::take(&mut *FAKES.lock().unwrap());
    drop(old);
}

pub fn call(id: BuiltinId, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    let sig = &SIGS[(id.0 - BASE) as usize];
    if sig.name == "render" {
        // render(path, name: value...): every named argument is a value
        return crate::text::render(args, names, span);
    }
    match sig.name {
        "ask" => return crate::ai::ask(args, names, span),
        "__ai_config" => return crate::ai::config(args, names, span),
        "__ai_models" => return crate::ai::models(args, names, span),
        "__ai_agent" => return crate::ai::agent(args, names, span),
        _ => {}
    }
    if sig.name == "log" {
        return log(args, names, span);
    }
    if sig.name == "__rate" {
        // __rate(amount, words...): the words of `10 per minute per ip`
        return crate::web::rate(args, span);
    }
    if let Some(m) = sig.name.strip_prefix("http.") {
        return crate::http::request(&m.to_uppercase(), args, names, span);
    }
    let a = check_args(sig, args, names, span)?;
    let x = |i: usize| a.get(i).unwrap();
    use crate::crypto as c;
    match sig.name {
        "move_file" => crate::text::move_file(x(0), x(1), span),
        "regex" => crate::text::regex(x(0), span),
        "secret" => c::secret(x(0), a.get(1), span),
        "password.hash" => c::password_hash(x(0), span),
        "password.check" => c::password_check(x(0), x(1), span),
        "password.generate" => c::password_generate(a.get(0), span),
        "key.new" => c::key_new(),
        "key.load" => c::key_load(x(0), span),
        "keypair.new" => c::keypair_new(),
        "keypair.load" => c::keypair_load(x(0), span),
        "encrypt" => c::encrypt(x(0), x(1), span),
        "decrypt" => c::decrypt(x(0), x(1), span),
        "encrypt_file" => c::encrypt_file(x(0), x(1), span),
        "decrypt_file" => c::decrypt_file(x(0), x(1), span),
        "sign" => c::sign(x(0), x(1), span),
        "verify" => c::verify(x(0), x(1), x(2), span),
        "connect" => crate::db::connect(x(0), a.get(1), span),
        "__db_table" => crate::db::register(x(0), x(1), x(2), x(3), span),
        "__db_begin" => crate::db::begin(x(0), span),
        "__db_commit" => crate::db::end(x(0), true, span),
        "__db_rollback" => crate::db::end(x(0), false, span),
        "__sql_text" => crate::db::sql_text(x(0), x(1), span),
        "jwt" => crate::web::jwt(x(0), span),
        "__ai_tool" => crate::ai::tool(x(0), x(1), x(2), span),
        "mcp" => crate::ai::mcp(span),
        "__c_library" => crate::clib::library(x(0), x(1), a.get(2).unwrap_or(NONE), span),
        "__python_import" => crate::pylib::import(x(0), a.get(1).unwrap_or(NONE), span),
        "__web_server" => crate::web::server(x(0), a.get(1), a.get(2), span),
        "__web_route" => crate::web::route(x(0), x(1), x(2), x(3), x(4), a.get(5), a.get(6), a.get(7), span),
        "__fake" => {
            let name = x(0).as_str().unwrap_or("").to_string();
            let value = crate::pipe::bind(x(1))?;
            FAKES.lock().unwrap().push((name, value));
            Ok(Val::none())
        }
        _ => unreachable!(),
    }
}

/// log(values...): a line on the standard error output, with the @private
/// fields of records hidden (section 9).
fn log(args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    if let Some(n) = names.iter().flatten().next() {
        let d = nexo_diag::Diagnostic::new_spanless("arg_unknown").arg("fn", "log").arg("param", *n).arg("params", "-");
        return Err(crate::state::diag(d, span));
    }
    let mut parts = Vec::new();
    for a in args {
        let v = crate::pipe::bind(*a)?;
        parts.push(crate::fmt::show_hidden(v.v(), span)?);
    }
    crate::state::flush();
    eprintln!("{}", parts.join(" "));
    Ok(Val::none())
}
