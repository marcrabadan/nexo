//! Builtins of the standard library added in phase 4 (section 15) and the
//! internal functions module blocks are rewritten into (D90). Their ids
//! start at BASE; `stdlib::call` sends them here.

use crate::state::R;
use crate::stdlib::{check_args, opt, p, Sig};
use crate::value::*;

pub const BASE: u32 = 100;

pub const B_MOVE_FILE: u32 = BASE;
pub const B_REGEX: u32 = BASE + 1;
pub const B_RENDER: u32 = BASE + 2;

static SIGS: &[Sig] = &[
    Sig { name: "move_file", params: &[p("path"), p("to")] },
    Sig { name: "regex", params: &[p("pattern")] },
    Sig { name: "render", params: &[p("path")] },
];

pub fn name(id: BuiltinId) -> &'static str {
    SIGS[(id.0 - BASE) as usize].name
}

/// Builtin names of the library as values.
pub fn value(name: &str) -> Option<Val> {
    let i = SIGS.iter().position(|s| s.name == name)?;
    Some(Val::new(Obj::Builtin(BuiltinId(BASE + i as u32))))
}

/// Members of the library's namespaces (`http.get`...).
pub fn member(_ns: &str, _name: &str, _span: u32) -> Option<R<Val>> {
    None
}

pub fn call(id: BuiltinId, args: &[V], names: &[Option<&str>], span: u32) -> R<Val> {
    if id.0 == B_RENDER {
        // render(path, name: value...): every named argument is a value
        return crate::text::render(args, names, span);
    }
    let sig = &SIGS[(id.0 - BASE) as usize];
    let a = check_args(sig, args, names, span)?;
    let _ = opt;
    match id.0 {
        B_MOVE_FILE => crate::text::move_file(a.get(0).unwrap(), a.get(1).unwrap(), span),
        B_REGEX => crate::text::regex(a.get(0).unwrap(), span),
        _ => unreachable!(),
    }
}
