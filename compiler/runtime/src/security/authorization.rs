//! Operator-owned agent/tool grants. Model output can select only an already
//! registered function; it never creates authority or an approval decision.
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use super::{context, Capability, Effect, Engine, Rule};
use crate::json::Json;
use crate::state::{diag, R};

type Identity = (PathBuf, String);
struct AgentGrant { permissions: Arc<Engine>, tools: HashSet<Identity> }
struct ToolGrant { permissions: Arc<Engine>, confirm: bool }
pub struct Configuration { agents: HashMap<Identity, AgentGrant>, tools: HashMap<Identity, ToolGrant>, state: HashMap<Identity,crate::agent_state::Grant>, fingerprint:String, path:PathBuf }
fn exact(j: &Json, fields: &[&str]) -> Result<(), String> {
    let Json::Obj(pairs) = j else { return Err("Expected authorization object".into()) };
    if pairs.len() != fields.len() || fields.iter().any(|key| pairs.iter().filter(|(k,_)| k == key).count() != 1) {
        return Err("Unknown, missing or duplicate authorization fields".into());
    }
    Ok(())
}
fn identity(name: &str, directory: &Path) -> Result<Identity, String> {
    let (file,symbol) = name.rsplit_once("::").ok_or("Use file.nexo::symbol identities")?;
    if symbol.is_empty() || !symbol.chars().all(|c| c.is_alphanumeric() || c == '_') { return Err("Invalid authorization symbol".into()); }
    let file = directory.join(file).canonicalize().map_err(|_| "Authorization identity does not exist")?;
    if !file.is_file() { return Err("Authorization identity must name a file".into()); }
    Ok((file,symbol.into()))
}
fn permissions(j: &Json) -> Result<Arc<Engine>, String> {
    let Json::Arr(entries) = j else { return Err("Permissions must be an array".into()) };
    let mut rules = Vec::new();
    for entry in entries {
        let Json::Arr(words) = entry else { return Err("Invalid permission rule".into()) };
        let words = words.iter().map(|w| w.str().ok_or("Permission words must be strings")).collect::<Result<Vec<_>,_>>()?;
        if words.len() < 2 { return Err("Incomplete permission rule".into()); }
        let effect = match words[0] { "allow" => Effect::Allow, "deny" => Effect::Deny, _ => return Err("Invalid permission effect".into()) };
        let cap = Capability::from_boundary(words[1],"").ok_or("Invalid permission capability")?;
        if (words[1] == "shell" && words.len() != 2) || (words[1] != "shell" && words.len() < 3) { return Err("Invalid permission targets".into()); }
        rules.push(Rule { effect, kind: cap.kind(), targets: words[2..].iter().map(|s| s.to_string()).collect() });
    }
    Ok(Arc::new(Engine::new(rules)))
}
/// An observation of a configured dispatch ceiling, never an authority token.
pub struct AnalysisDecision {
    pub scope: &'static str, pub file: PathBuf, pub symbol: String,
    pub allowed: bool, pub confirm: bool,
}
impl Configuration {
    pub fn analysis_registrations(&self, file: &Path, symbol: &str) -> Vec<(&'static str,PathBuf,String)> {
        let key = (file.to_path_buf(),symbol.to_string());
        if !self.tools.contains_key(&key) { return vec![]; }
        let mut out = vec![("tool",key.0.clone(),key.1.clone())];
        out.extend(self.agents.iter().filter(|(_,a)|a.tools.contains(&key)).map(|(id,_)|("agent",id.0.clone(),id.1.clone())));
        out
    }
    pub fn analysis_agent_files(&self, file: &Path, symbol: &str) -> Vec<PathBuf> {
        let id = (file.to_path_buf(),symbol.to_string());
        self.agents.iter().filter(|(_,a)|a.tools.contains(&id)).map(|((file,_),_)|file.clone()).collect()
    }
    pub fn analysis_confirmation(&self, file: &Path, symbol: &str) -> bool {
        self.tools.get(&(file.to_path_buf(),symbol.to_string())).is_some_and(|t|t.confirm)
    }
    pub fn analysis_has_tool(&self, file: &Path, symbol: &str) -> bool {
        self.tools.contains_key(&(file.to_path_buf(),symbol.to_string()))
    }
    /// Inspect configured tool/agent scenarios without entering a runtime scope.
    pub fn analysis_checks(&self, file: &Path, symbol: &str, cap: Capability<'_>) -> Vec<AnalysisDecision> {
        let id = (file.to_path_buf(),symbol.to_string());
        let Some(tool) = self.tools.get(&id) else { return vec![] };
        let mut out = vec![AnalysisDecision { scope: "tool", file: file.to_path_buf(), symbol: symbol.into(),
            allowed: !tool.permissions.evaluate(cap,true,true).denied(), confirm: tool.confirm }];
        let mut matched_agent = false;
        for ((file,symbol),agent) in &self.agents {
            if agent.tools.contains(&id) {
                matched_agent = true;
                out.push(AnalysisDecision { scope: "agent", file: file.clone(), symbol: symbol.clone(),
                    allowed: !agent.permissions.evaluate(cap,true,true).denied(), confirm: false });
            }
        }
        if !matched_agent { out.push(AnalysisDecision { scope: "dispatch", file:file.to_path_buf(),symbol:symbol.into(),allowed:false,confirm:false }); }
        out
    }
    pub fn load(path: &Path) -> Result<Self,String> {
        use std::io::Read;
        let mut text = String::new();
        std::fs::File::open(path).map_err(|_| "Cannot read authorization file")?.take(1048577)
            .read_to_string(&mut text).map_err(|_| "Cannot read authorization file")?;
        if text.len() > 1048576 { return Err("Authorization exceeds 1 MiB".into()); }
        // The shared JSON parser is recursive. Reject deep hostile manifests
        // before parsing, with quoted delimiters and escapes handled correctly.
        let (mut depth,mut quote,mut escape) = (0i32,false,false);
        for b in text.bytes() {
            if quote { if escape { escape=false; } else if b == b'\\' { escape=true; } else if b == b'"' { quote=false; } }
            else if b == b'"' { quote=true; }
            else if b == b'[' || b == b'{' { depth+=1; if depth>16 { return Err("Authorization is too deeply nested".into()); } }
            else if b == b']' || b == b'}' { depth-=1; if depth<0 { return Err("Invalid authorization JSON".into()); } }
        }
        let doc = crate::json::parse_unique(&text).ok_or("Invalid authorization JSON")?;
        let version=doc.get("version").int();
        exact(&doc,if version==Some(2){&["version","agents","tools","state"]}else{&["version","agents","tools"]})?;
        if !matches!(version,Some(1|2)) { return Err("Unknown authorization version".into()); }
        let directory = path.parent().unwrap_or(Path::new("."));
        let Json::Obj(entries) = doc.get("tools") else { return Err("Invalid tool grants".into()) };
        let mut tools = HashMap::new();
        for (name,grant) in entries {
            exact(grant,&["permissions","confirm"])?;
            let id = identity(name,directory)?;
            let grant = ToolGrant { permissions: permissions(grant.get("permissions"))?, confirm: grant.get("confirm").bool().ok_or("confirm must be boolean")? };
            if tools.insert(id,grant).is_some() { return Err("Duplicate canonical tool identity".into()); }
        }
        let Json::Obj(entries) = doc.get("agents") else { return Err("Invalid agent grants".into()) };
        let mut agents = HashMap::new();
        for (name,grant) in entries {
            exact(grant,&["permissions","tools"])?;
            let Json::Arr(names) = grant.get("tools") else { return Err("Agent tools must be an array".into()) };
            let mut permitted = HashSet::new();
            for name in names {
                let id = identity(name.str().ok_or("Tool identity must be a string")?,directory)?;
                if !tools.contains_key(&id) || !permitted.insert(id) { return Err("Unknown or duplicate agent tool identity".into()); }
            }
            let id = identity(name,directory)?;
            let grant = AgentGrant { permissions: permissions(grant.get("permissions"))?, tools: permitted };
            if agents.insert(id,grant).is_some() { return Err("Duplicate canonical agent identity".into()); }
        }
        let mut state=HashMap::new();
        if version==Some(2) {
            let Json::Obj(entries)=doc.get("state") else{return Err("Invalid state grants".into())};
            for (name,value) in entries {
                let id=identity(name,directory)?;
                if !agents.contains_key(&id)||state.contains_key(&id){return Err("Invalid state identity".into())}
                let grant=crate::agent_state::parse(value,directory)?;
                for old in state.values() {
                    let old: &crate::agent_state::Grant=old;
                    if (old.directory==grant.directory&&old!=&grant) || (old.directory!=grant.directory&&(old.directory.starts_with(&grant.directory)||grant.directory.starts_with(&old.directory))){return Err("Conflicting state roots".into())}
                }
                state.insert(id,grant);
            }
        }
        Ok(Self { agents, tools, state, fingerprint:crate::agent_state::digest(&text),path:path.canonicalize().map_err(|_|"Invalid authorization path")? })
    }
}
struct State { config: Configuration, files: HashMap<u32,PathBuf>, protected:Vec<PathBuf> }
static STATE: OnceLock<State> = OnceLock::new();
pub fn active() -> bool { STATE.get().is_some() }
pub fn initialize(root: &str) -> Result<(),String> {
    let Some(path) = std::env::var_os("NEXO_SECURITY_AUTHORIZATION") else { return Ok(()) };
    let config = Configuration::load(Path::new(&path))?;
    let mut files = nexo_diag::registered_files().into_iter().filter_map(|(id,name)| Path::new(name).canonicalize().ok().map(|p| (id,p))).collect::<HashMap<_,_>>();
    files.insert(0,Path::new(root).canonicalize().map_err(|_| "Cannot freeze root identity")?);
    let mut protected=vec![config.path.clone()];
    if let Some(path)=std::env::var_os("NEXO_SECURITY_DEPENDENCIES"){protected.push(Path::new(&path).canonicalize().map_err(|_|"Cannot pin dependency configuration")?);}
    STATE.set(State { config, files, protected }).map_err(|_| "Authorization already initialized".into())
}
fn identity_at(name: &str, span: u32) -> Option<Identity> {
    let state = STATE.get()?;
    let file = crate::state::span(span)?.file;
    Some((state.files.get(&file)?.clone(),name.to_string()))
}
#[derive(Clone)]
pub struct Seal { id: Option<Identity>, snapshot: context::Snapshot }
pub fn seal(name: &str, span: u32) -> Seal { Seal { id: identity_at(name,span), snapshot: context::capture() } }
fn denied(span: u32) -> crate::state::Fail { diag(nexo_diag::Diagnostic::new_spanless("security_dispatch"),span) }
/// Agents with dependency ceilings require explicit authorization configuration.
/// Ordinary execution without either configuration preserves its old behavior.
pub fn enter_agent(seal: &Seal, span: u32) -> R<Option<context::Scope>> {
    let Some(state) = STATE.get() else {
        return if std::env::var_os("NEXO_SECURITY_DEPENDENCIES").is_some() { Err(denied(span)) } else { Ok(None) };
    };
    let grant = seal.id.as_ref().and_then(|id| state.config.agents.get(id)).ok_or_else(|| denied(span))?;
    Ok(Some(context::attenuate(&seal.snapshot,grant.permissions.clone())))
}
pub fn enter_tool(agent: &Seal, tool: &Seal, span: u32) -> R<(Option<context::Scope>,bool)> {
    let Some(state) = STATE.get() else { return Ok((None,false)) };
    let a = agent.id.as_ref().and_then(|id| state.config.agents.get(id)).ok_or_else(|| denied(span))?;
    let id = tool.id.as_ref().filter(|id| a.tools.contains(id)).ok_or_else(|| denied(span))?;
    let grant = state.config.tools.get(id).ok_or_else(|| denied(span))?;
    Ok((Some(context::attenuate(&tool.snapshot,grant.permissions.clone())),grant.confirm))
}
pub fn check_confirmation(required: bool, span: u32) -> R<()> {
    // OS-profile stdin carries broker/control IPC, never a human approval.
    if required && (std::env::var_os("NEXO_SECURITY_SANDBOX").is_some() || super::ingress::port().is_some()) { return Err(denied(span)); }
    Ok(())
}

pub fn state_grant(seal:&Seal,span:u32)->R<(crate::agent_state::Grant,String,String)>{
    let state=STATE.get().ok_or_else(||state_error(span))?;
    let id=seal.id.as_ref().ok_or_else(||state_error(span))?;
    let grant=state.config.state.get(id).ok_or_else(||state_error(span))?;
    Ok((grant.clone(),state.config.fingerprint.clone(),format!("{}::{}",id.0.display(),id.1)))
}
pub fn state_error(span:u32)->crate::state::Fail{diag(nexo_diag::Diagnostic::new_spanless("agent_state"),span)}
pub fn protect(kind:&str,value:&str,span:u32)->R<()>{
    let Some(state)=STATE.get() else{return Ok(())};
    if kind=="write"&&!state.config.state.is_empty(){let path=PathBuf::from(super::matching::resolve(value));if state.protected.iter().any(|file|file.starts_with(&path)){return Err(state_error(span))}}
    for grant in state.config.state.values(){
        let mut denied=kind=="env"&&value==grant.key;
        if kind=="read"||kind=="write" {
            let path=PathBuf::from(super::matching::resolve(value));
            denied |= path.starts_with(&grant.directory)||(kind=="write"&&grant.directory.starts_with(&path));
        }
        if denied{return Err(state_error(span))}
    }
    Ok(())
}

pub fn has_state(seal:&Seal)->bool{STATE.get().is_some_and(|s|seal.id.as_ref().is_some_and(|id|s.config.state.contains_key(id)))}


pub(crate) fn context_identity(seal:&Seal)->String {
    let mut grants=Vec::new();if let Some(state)=STATE.get(){
        for (id,g) in &state.config.agents{grants.push(format!("{}::{}:{}",id.0.display(),id.1,g.permissions.identity()));}
        for (id,g) in &state.config.tools{grants.push(format!("{}::{}:{}",id.0.display(),id.1,g.permissions.identity()));}
    }grants.sort();
    crate::agent_state::digest(&format!("{:?}:{}:{}:{}:{:?}",std::env::current_dir().ok(),crate::policy::identity(),context::identity(&context::capture()),context::identity(&seal.snapshot),grants))
}
