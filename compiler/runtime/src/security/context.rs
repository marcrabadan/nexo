//! Immutable dependency ceilings. Every active caller ceiling is intersected.
//! This first context profile supports synchronous Nexo code only; launchers
//! reject asynchronous constructs before execution, rather than dropping context.
use std::cell::RefCell;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use super::{Capability, Effect, Engine, Rule};
use crate::json::Json;

pub struct Configuration { modules: HashMap<PathBuf, Arc<Engine>> }
impl Configuration {
    pub fn load(path: &Path, root: &Path) -> Result<Self, String> {
        use std::io::Read;
        let mut text = String::new();
        std::fs::File::open(path).map_err(|_| "Cannot read dependency permissions")?.take(1048577)
            .read_to_string(&mut text).map_err(|_| "Cannot read dependency permissions")?;
        if text.len() > 1048576 { return Err("Dependency permissions exceed 1 MiB".into()); }
        let json = crate::json::parse(&text).ok_or("Invalid dependency permission JSON")?;
        let Json::Obj(fields) = &json else { return Err("Dependency permissions must be an object".into()); };
        if fields.len() != 2 || json.get("version").int() != Some(1) || fields.iter().any(|(k,_)| k != "version" && k != "modules") {
            return Err("Dependency permissions require exactly version=1 and modules".into());
        }
        let Json::Obj(modules) = json.get("modules") else { return Err("modules must be an object".into()); };
        let mut out = HashMap::new();
        let root = root.canonicalize().map_err(|_| "Cannot resolve application identity")?;
        for (name, entries) in modules {
            let file = path.parent().unwrap_or(Path::new(".")).join(name).canonicalize().map_err(|_| "A dependency identity does not exist")?;
            if file == root || out.contains_key(&file) { return Err("Duplicate dependency identity or application override".into()); }
            let Json::Arr(entries) = entries else { return Err("Dependency rules must be arrays".into()); };
            let mut rules = Vec::new();
            for entry in entries {
                let Json::Arr(words) = entry else { return Err("Dependency rule must be an array".into()); };
                let words: Vec<&str> = words.iter().map(|v| v.str().ok_or("Rule words must be strings")).collect::<Result<_,_>>()?;
                if words.len() < 2 { return Err("Incomplete dependency rule".into()); }
                let effect = match words[0] { "allow" => Effect::Allow, "deny" => Effect::Deny, _ => return Err("Invalid dependency rule effect".into()) };
                let cap = Capability::from_boundary(words[1], "").ok_or("Unknown dependency capability")?;
                if (words[1] == "shell" && words.len() != 2) || (words[1] != "shell" && words.len() < 3) { return Err("Invalid dependency targets".into()); }
                rules.push(Rule { effect, kind: cap.kind(), targets: words[2..].iter().map(|s| s.to_string()).collect() });
            }
            out.insert(file, Arc::new(Engine::new(rules)));
        }
        Ok(Self { modules: out })
    }
}
struct Contexts { files: HashMap<u32, Arc<Engine>>, empty: Arc<Engine> }
static INSTRUMENTED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
pub fn instrumented() { INSTRUMENTED.store(true, std::sync::atomic::Ordering::Relaxed); }
static CONTEXTS: OnceLock<Contexts> = OnceLock::new();
thread_local! { static STACK: RefCell<Vec<Arc<Engine>>> = const { RefCell::new(Vec::new()) }; }

pub fn initialize(root: &str) -> Result<(), String> {
    let Some(path) = std::env::var_os("NEXO_SECURITY_DEPENDENCIES") else { return Ok(()) };
    if !INSTRUMENTED.load(std::sync::atomic::Ordering::Relaxed) { return Err("Binary was not compiled with dependency context guards".into()); }
    let config = Configuration::load(Path::new(&path), Path::new(root))?;
    let empty = Arc::new(Engine::new(Vec::new()));
    let files = nexo_diag::registered_files().into_iter().map(|(id,name)| {
        let engine = Path::new(name).canonicalize().ok().and_then(|p| config.modules.get(&p).cloned()).unwrap_or_else(|| empty.clone());
        (id,engine)
    }).collect();
    CONTEXTS.set(Contexts { files, empty }).map_err(|_| "Dependency contexts already initialized".into())
}

pub fn enter(file: u32) -> u64 {
    let Some(contexts) = CONTEXTS.get() else { return u64::MAX };
    STACK.with(|s| {
        let mut s = s.borrow_mut(); let depth = s.len() as u64;
        if file != 0 { s.push(contexts.files.get(&file).unwrap_or(&contexts.empty).clone()); }
        depth
    })
}
pub fn restore(depth: u64) {
    if depth != u64::MAX { STACK.with(|s| s.borrow_mut().truncate(depth as usize)); }
}
pub fn allows(capability: Capability<'_>, file: u32) -> bool {
    let Some(contexts) = CONTEXTS.get() else { return true };
    let permits = |e: &Engine| !e.evaluate(capability, true, true).denied();
    if file != 0 && !permits(contexts.files.get(&file).unwrap_or(&contexts.empty)) { return false; }
    STACK.with(|s| s.borrow().iter().all(|e| permits(e)))
}

/// Until callable/opaque handles have sealed contexts, dependency boundaries
/// export plain data only. This also rejects deferred computations in containers.
pub fn check_result(value: crate::value::V, span: u32) {
    if CONTEXTS.get().is_none() || STACK.with(|s| s.borrow().is_empty()) { return; }
    fn data(v: crate::value::V, depth: usize, budget: &mut usize) -> bool {
        use crate::value::*;
        if depth > 128 || *budget == 0 { return false; }
        *budget -= 1;
        match v.get() {
            None => matches!(v.tag,T_NONE|T_BOOL|T_INT|T_FLOAT|T_LITFLOAT|T_SSTR),
            Some(Obj::Str(_) | Obj::Decimal(_) | Obj::Money(..) | Obj::Quantity(..) | Obj::Currency(_)) => true,
            Some(Obj::List(items)) => items.borrow().iter().all(|v| data(v,depth+1,budget)),
            Some(Obj::Map(items)) => items.borrow().entries.iter().all(|(k,v)| data(k.v(),depth+1,budget) && data(v.v(),depth+1,budget)),
            Some(Obj::Set(items)) => items.borrow().items.iter().all(|v| data(v.v(),depth+1,budget)),
            _ => false,
        }
    }
    if !data(value,0,&mut 100000) {
        let _ = crate::state::diag(nexo_diag::Diagnostic::new_spanless("security_context_escape"),span);
    }
}
