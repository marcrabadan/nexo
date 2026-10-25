//! nexo.ci (section 12, D122, D123): `pipeline` blocks, `nexo ci run`,
//! which runs their steps here, and `nexo ci export`, which writes the file
//! of a CI service that runs each step with `nexo ci run --step`. Follows
//! phase 0's ci.py.

use std::io::Write;
use std::sync::Mutex;

use nexo_diag::{msg, suggest, Diagnostic};

use crate::call::call_value;
use crate::cfg::{as_cfg, atom_text, Body, Cfg};
use crate::state::{diag, failure_as, R};
use crate::value::*;

const STATE_DIR: &str = ".nexo/ci";

pub struct Pipeline {
    pub name: String,
    pub span: u32,
    /// ("push", Some(branch) | None) | ("pull_request", None)
    pub triggers: Vec<(String, Option<String>)>,
    pub secrets: Vec<String>,
    pub version: Option<String>,
    pub steps: Vec<(String, Val)>,
    pub after: Vec<(String, Val)>,
    /// the entries stay alive while the pipeline is declared
    pub entries: Val,
}

static PIPELINES: Mutex<Vec<&'static Pipeline>> = Mutex::new(Vec::new());

/// The run in progress: what the steps keep, the version, the step.
struct RunState {
    vars: Vec<(String, Val)>,
    yes: bool,
    failed: Option<String>,
}

static RUN: Mutex<Option<RunState>> = Mutex::new(None);
static ACTIVE: Mutex<u32> = Mutex::new(0);

/// The value the functions of the steps receive.
pub struct Run {
    pub pipeline: String,
}

impl Native for Run {
    fn type_name(&self) -> &str {
        "run"
    }
    fn repr(&self) -> String {
        format!("run({})", self.pipeline)
    }
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

pub fn release_all() {
    let old = std::mem::take(&mut *PIPELINES.lock().unwrap());
    for p in old {
        // SAFETY: every entry is a box leaked by pipeline(), and no run uses it now
        drop(unsafe { Box::from_raw(p as *const Pipeline as *mut Pipeline) });
    }
    let run = RUN.lock().unwrap().take();
    drop(run);
}

fn m(key: &str, args: &[(&str, String)]) -> String {
    msg(key, args)
}

fn say(text: &str) {
    crate::state::write_out(&format!("{text}\n"));
    crate::state::flush();
}

fn err(d: Diagnostic, span: u32) -> crate::state::Fail {
    diag(d, span)
}

// ---------------------------------------------------------------- blocks

fn is_word(v: &Val, w: &str) -> bool {
    v.v().as_str() == Some(w)
}

fn entry_text(e: &Cfg) -> String {
    e.repr()
}

pub fn pipeline(name: V, entries: V, span: u32) -> R<Val> {
    let name = name.as_str().unwrap_or("").to_string();
    if PIPELINES.lock().unwrap().iter().any(|p| p.name == name) {
        return Err(err(Diagnostic::new_spanless("duplicate_name").arg("name", name), span));
    }
    let mut p = Pipeline {
        name: name.clone(),
        span,
        triggers: Vec::new(),
        secrets: Vec::new(),
        version: None,
        steps: Vec::new(),
        after: Vec::new(),
        entries: Val::from(entries),
    };
    for item in crate::cfg::items(entries) {
        let Some(e) = as_cfg(item.v()) else { continue };
        let w = e.words();
        let key = e.key();
        let code = match &e.body {
            Body::Code(f) => Some(f.clone()),
            _ => None,
        };
        let plain = matches!(e.body, Body::None) && e.options.is_empty();
        if w.first().is_some_and(|x| is_word(x, "trigger")) && e.values.is_none() && plain && e.head.len() == 1 {
            let rest: Vec<Option<&str>> = w[1..].iter().map(|x| x.v().as_str()).collect();
            match rest.as_slice() {
                [Some("pull_request")] => p.triggers.push(("pull_request".into(), None)),
                [Some("push")] => p.triggers.push(("push".into(), None)),
                [Some("push"), Some("to"), Some(b)] => p.triggers.push(("push".into(), Some(b.to_string()))),
                _ => return Err(err(Diagnostic::new_spanless("ci_trigger").arg("entry", entry_text(e)), e.span)),
            }
        } else if key == Some("secrets") && plain {
            let v = e.values.as_ref().unwrap();
            if v.len() == 1 && v[0].len() == 3 && is_word(&v[0][1], "from") && is_word(&v[0][2], "vault") {
                return Err(err(Diagnostic::new_spanless("not_supported").arg("what", "\"secrets = [...] from vault\""), e.span));
            }
            let names = if v.len() == 1 && v[0].len() == 1 { Some(&v[0][0]) } else { None };
            let ok_name = |s: &str| {
                let mut c = s.chars();
                c.next().is_some_and(|f| f.is_ascii_uppercase() || f == '_') && c.all(|x| x.is_ascii_uppercase() || x.is_ascii_digit() || x == '_')
            };
            let list = names.and_then(|n| match n.v().get() {
                Some(Obj::List(items)) => {
                    let items = items.borrow();
                    let texts: Vec<Option<&str>> = items.iter().map(|x| x.v().as_str()).collect();
                    if texts.iter().all(|t| t.is_some_and(ok_name)) {
                        Some(texts.into_iter().map(|t| t.unwrap().to_string()).collect::<Vec<_>>())
                    } else {
                        None
                    }
                }
                _ => None,
            });
            match list {
                Some(l) => p.secrets = l,
                None => return Err(err(Diagnostic::new_spanless("ci_secrets"), e.span)),
            }
        } else if key == Some("version") && plain {
            let v = e.value().unwrap();
            match v.v().as_str() {
                Some(s) if s == "auto" || is_version(s) => p.version = Some(s.to_string()),
                _ => return Err(err(Diagnostic::new_spanless("ci_version").arg("value", atom_text(v.v())), e.span)),
            }
        } else if w.len() == 2 && is_word(&w[0], "step") && code.is_some() && w[1].v().as_str().is_some() {
            let step = w[1].v().as_str().unwrap().to_string();
            if p.steps.iter().any(|(s, _)| *s == step) {
                return Err(err(Diagnostic::new_spanless("duplicate_name").arg("name", step), e.span));
            }
            p.steps.push((step, code.unwrap()));
        } else if w.len() == 2 && is_word(&w[0], "after") && (is_word(&w[1], "success") || is_word(&w[1], "failure")) && code.is_some() {
            let what = w[1].v().as_str().unwrap().to_string();
            if p.after.iter().any(|(s, _)| *s == what) {
                return Err(err(Diagnostic::new_spanless("duplicate_name").arg("name", format!("after {what}")), e.span));
            }
            p.after.push((what, code.unwrap()));
        } else {
            return Err(err(Diagnostic::new_spanless("ci_entry").arg("entry", entry_text(e)), e.span));
        }
    }
    if p.steps.is_empty() {
        return Err(err(Diagnostic::new_spanless("ci_no_steps").arg("name", name), span));
    }
    PIPELINES.lock().unwrap().push(Box::leak(Box::new(p)));
    Ok(Val::none())
}

fn is_version(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    parts.len() == 3 && parts.iter().all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()))
}

pub fn ci_get(name: V, span: u32) -> R<Val> {
    let name = name.as_str().unwrap_or("");
    let found = RUN.lock().unwrap().as_ref().and_then(|r| r.vars.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone()));
    match found {
        Some(v) => Ok(v),
        None => Err(err(Diagnostic::new_spanless("ci_missing").arg("name", name), span)),
    }
}

pub fn ci_keep(name: V, value: V, span: u32) -> R<Val> {
    let name = name.as_str().unwrap_or("").to_string();
    let value = crate::pipe::bind(value)?;
    json_value(&name, value.v(), span)?;
    if let Some(r) = RUN.lock().unwrap().as_mut() {
        set_var(&mut r.vars, &name, value);
    }
    Ok(Val::none())
}

fn set_var(vars: &mut Vec<(String, Val)>, name: &str, value: Val) {
    match vars.iter_mut().find(|(k, _)| k == name) {
        Some(slot) => slot.1 = value,
        None => vars.push((name.to_string(), value)),
    }
}

/// What a step keeps travels between the jobs of a CI as JSON.
fn json_value(name: &str, v: V, span: u32) -> R<()> {
    let ok = match v.tag {
        T_NONE | T_BOOL | T_INT | T_FLOAT => true,
        _ => match v.get() {
            Some(Obj::Str(_)) => true,
            Some(Obj::List(items)) => {
                for x in items.borrow().iter() {
                    json_value(name, x.v(), span)?;
                }
                true
            }
            Some(Obj::Map(mp)) => {
                let mp = mp.borrow();
                if mp.entries.iter().all(|(k, _)| k.v().as_str().is_some()) {
                    for (_, x) in mp.entries.iter() {
                        json_value(name, x.v(), span)?;
                    }
                    true
                } else {
                    false
                }
            }
            _ => false,
        },
    };
    if ok {
        Ok(())
    } else {
        Err(err(Diagnostic::new_spanless("ci_value").arg("name", name).arg("vtype", crate::fmt::type_name(v)), span))
    }
}

// ------------------------------------------------------------ functions

fn current(fname: &str, span: u32) -> R<()> {
    if *ACTIVE.lock().unwrap() == 0 {
        return Err(err(Diagnostic::new_spanless("ci_outside").arg("fn", fname), span));
    }
    Ok(())
}

fn var_text(name: &str) -> String {
    RUN.lock()
        .unwrap()
        .as_ref()
        .and_then(|r| r.vars.iter().find(|(k, _)| k == name).and_then(|(_, v)| v.v().as_str().map(String::from)))
        .unwrap_or_default()
}

/// Run a command with the shell; what it writes is shown (stdout and
/// stderr together) and returned.
fn shell(command: &str) -> (i32, String) {
    crate::state::flush();
    let _ = std::io::stdout().flush();
    let out = std::process::Command::new("sh").arg("-c").arg(format!("exec 2>&1\n{command}")).stdin(std::process::Stdio::inherit()).output();
    let (code, text) = match out {
        Ok(o) => (o.status.code().unwrap_or(1), String::from_utf8_lossy(&o.stdout).into_owned()),
        Err(e) => (127, format!("{e}\n")),
    };
    if !text.is_empty() {
        crate::state::write_out(&text);
        if !text.ends_with('\n') {
            crate::state::write_out("\n");
        }
    }
    (code, text)
}

pub fn run(command: V, span: u32) -> R<Val> {
    let Some(c) = command.as_str() else { return Err(crate::stdlib::expects("run", "str", command, span)) };
    let (code, out) = shell(c);
    if code != 0 {
        return Err(failure_as("Error", "ci_run", span, &[("command", c.to_string()), ("code", code.to_string())]));
    }
    Ok(Val::str(out.strip_suffix('\n').unwrap_or(&out).to_string()))
}

pub fn test(span: u32) -> R<Val> {
    current("test", span)?;
    let driver = std::env::var("NEXO_DRIVER").unwrap_or_else(|_| "nexo".into());
    crate::state::flush();
    // stdout and stderr together, as they are written
    let out = std::process::Command::new("sh").arg("-c").arg(format!("exec 2>&1\n'{}' test", driver.replace('\'', "'\\''"))).output();
    let ok = match out {
        Ok(o) => {
            crate::state::write_out(&String::from_utf8_lossy(&o.stdout));
            o.status.success()
        }
        Err(_) => false,
    };
    if !ok {
        return Err(failure_as("Error", "ci_tests", span, &[]));
    }
    Ok(Val::none())
}

pub fn build_image(name: V, span: u32) -> R<Val> {
    current("build_image", span)?;
    let valid = name.as_str().is_some_and(|n| {
        let mut c = n.chars();
        c.next().is_some_and(|f| f.is_ascii_lowercase() || f.is_ascii_digit())
            && c.all(|x| x.is_ascii_lowercase() || x.is_ascii_digit() || "._/-".contains(x))
    });
    if !valid {
        return Err(crate::stdlib::expects("build_image", "str", name, span));
    }
    let tag = format!("{}:{}", name.as_str().unwrap(), var_text("version"));
    let tool = std::env::var("NEXO_CONTAINER_TOOL").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "docker".into());
    let command = format!("{tool} build -t {tag} .");
    let (code, _) = shell(&command);
    if code != 0 {
        return Err(failure_as("Error", "ci_run", span, &[("command", command), ("code", code.to_string())]));
    }
    Ok(Val::str(tag))
}

fn infra_command(fname: &str, command: &str, stage: Option<V>, image: Option<V>, span: u32) -> R<i32> {
    current(fname, span)?;
    let mut texts = Vec::new();
    for v in [stage, image] {
        match v {
            Some(v) if !v.is_none() => match v.as_str() {
                Some(s) => texts.push(Some(s.to_string())),
                None => return Err(crate::stdlib::expects(fname, "str", v, span)),
            },
            _ => texts.push(None),
        }
    }
    if crate::infra::infras().is_empty() {
        let file = crate::state::program().file;
        return Err(err(Diagnostic::new_spanless("infra_none").arg("file", file), span));
    }
    crate::state::flush();
    *crate::infra::IMAGE.lock().unwrap() = texts[1].clone();
    let r = crate::infra_cloud::command(command, &[], texts[0].as_deref(), true);
    *crate::infra::IMAGE.lock().unwrap() = None;
    r
}

pub fn apply(stage: Option<V>, image: Option<V>, span: u32) -> R<Val> {
    if infra_command("apply", "apply", stage, image, span)? != 0 {
        return Err(failure_as("Error", "ci_apply", span, &[]));
    }
    Ok(Val::none())
}

pub fn plan(stage: Option<V>, image: Option<V>, span: u32) -> R<Val> {
    crate::infra_cloud::CAPTURE.lock().unwrap().push(Vec::new());
    let r = infra_command("plan", "plan", stage, image, span);
    let lines = crate::infra_cloud::CAPTURE.lock().unwrap().pop().unwrap_or_default();
    r?;
    Ok(Val::str(lines.join("\n")))
}

pub fn approve(by: V, span: u32) -> R<Val> {
    current("approve", span)?;
    let names: Option<Vec<String>> = match by.get() {
        Some(Obj::List(items)) => {
            let items = items.borrow();
            let t: Vec<Option<&str>> = items.iter().map(|x| x.v().as_str()).collect();
            if !t.is_empty() && t.iter().all(|x| x.is_some()) {
                Some(t.into_iter().map(|x| x.unwrap().to_string()).collect())
            } else {
                None
            }
        }
        _ => None,
    };
    let Some(names) = names else { return Err(crate::stdlib::expects("approve", "list[str]", by, span)) };
    let names = names.join(", ");
    let step = var_text("step");
    let yes = RUN.lock().unwrap().as_ref().is_some_and(|r| r.yes);
    if yes {
        crate::state::write_out(&format!("{}\n", m("info.ci_approved", &[("step", step), ("names", names)])));
        return Ok(Val::none());
    }
    crate::state::write_out(&m("info.ci_approve", &[("step", step.clone()), ("names", names)]));
    crate::state::flush();
    let mut line = String::new();
    let _ = std::io::stdin().read_line(&mut line);
    let answer = line.trim().to_lowercase();
    if !["yes", "y"].contains(&answer.as_str()) {
        if !line.ends_with('\n') {
            crate::state::write_out("\n");
        }
        return Err(failure_as("Error", "ci_approve", span, &[("step", step)]));
    }
    Ok(Val::none())
}

pub fn comment(text: V, span: u32) -> R<Val> {
    current("comment", span)?;
    let v = crate::pipe::bind(text)?;
    let text = crate::fmt::show(v.v());
    crate::state::write_out(&format!("{}\n{text}\n", m("info.ci_comment", &[])));
    let _ = std::fs::create_dir_all(STATE_DIR);
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(format!("{STATE_DIR}/comment.md")) {
        let _ = writeln!(f, "{text}");
    }
    Ok(Val::none())
}

pub fn unavailable(name: &str, span: u32) -> R<Val> {
    Err(err(Diagnostic::new_spanless("not_supported").arg("what", format!("\"{name}()\"")), span))
}

// --------------------------------------------------------------- version

fn git(args: &[&str]) -> (bool, String) {
    match std::process::Command::new("git").args(args).stderr(std::process::Stdio::null()).output() {
        Ok(o) => (o.status.success(), String::from_utf8_lossy(&o.stdout).into_owned()),
        Err(_) => (false, String::new()),
    }
}

fn tag_version(t: &str) -> Option<(u64, u64, u64)> {
    let rest = t.strip_prefix('v')?;
    if !is_version(rest) {
        return None;
    }
    let p: Vec<u64> = rest.split('.').map(|x| x.parse().unwrap_or(0)).collect();
    Some((p[0], p[1], p[2]))
}

/// The last tag vX.Y.Z raised by the commits after it: fix -> patch,
/// feat -> minor, `!` or BREAKING CHANGE -> major (D122).
fn auto_version() -> R<String> {
    if !git(&["rev-parse", "--git-dir"]).0 {
        return Err(err(Diagnostic::new_spanless("ci_git"), 0));
    }
    let (_, out) = git(&["tag", "--merged", "HEAD", "--list", "v[0-9]*"]);
    let (mut best, mut tag) = ((0, 0, 0), None);
    for t in out.split_whitespace() {
        if let Some(v) = tag_version(t) {
            if v >= best {
                best = v;
                tag = Some(t.to_string());
            }
        }
    }
    let range = match &tag {
        Some(t) => format!("{t}..HEAD"),
        None => "HEAD".into(),
    };
    let (_, out) = git(&["log", "--format=%B%x00", &range]);
    let mut bump = 0;
    for message in out.split('\0') {
        let message = message.trim();
        if message.is_empty() {
            continue;
        }
        let (kind, bang) = commit_head(message);
        let breaking = message.lines().any(|l| l.starts_with("BREAKING CHANGE:") || l.starts_with("BREAKING-CHANGE:"));
        if bang || breaking {
            bump = 3;
        } else if kind == Some("feat") {
            bump = bump.max(2);
        } else if kind == Some("fix") {
            bump = bump.max(1);
        }
    }
    let (mut x, mut y, mut z) = best;
    match bump {
        3 => (x, y, z) = (x + 1, 0, 0),
        2 => (y, z) = (y + 1, 0),
        1 => z += 1,
        _ => {}
    }
    Ok(format!("{x}.{y}.{z}"))
}

/// `type(scope)!:` at the start of a commit message: (type, has `!`).
fn commit_head(message: &str) -> (Option<&str>, bool) {
    let end = message.find(|c: char| !(c.is_alphanumeric() || c == '_')).unwrap_or(message.len());
    if end == 0 {
        return (None, false);
    }
    let (word, mut rest) = message.split_at(end);
    if let Some(r) = rest.strip_prefix('(') {
        match r.find(')') {
            Some(i) => rest = &r[i + 1..],
            None => return (None, false),
        }
    }
    let bang = rest.starts_with('!');
    if bang {
        rest = &rest[1..];
    }
    if rest.starts_with(':') {
        (Some(word), bang)
    } else {
        (None, false)
    }
}

fn version_of(p: &Pipeline) -> R<String> {
    match p.version.as_deref() {
        None => Ok("dev".into()),
        Some("auto") => auto_version(),
        Some(v) => Ok(v.to_string()),
    }
}

// ------------------------------------------------------------------ run

fn state_path(p: &Pipeline) -> String {
    format!("{STATE_DIR}/{}.json", p.name)
}

fn load_state(p: &Pipeline) -> Option<(Vec<(String, Val)>, Option<String>)> {
    let text = std::fs::read_to_string(state_path(p)).ok()?;
    let v = crate::data::json_parse(&text, None, 0).ok();
    let Some(v) = v else {
        crate::state::clear();
        return None;
    };
    let Some(Obj::Map(mp)) = v.v().get() else { return None };
    let mp = mp.borrow();
    let mut vars = Vec::new();
    let mut failed = None;
    for (k, x) in mp.entries.iter() {
        match k.v().as_str() {
            Some("vars") => {
                if let Some(Obj::Map(vm)) = x.v().get() {
                    for (vk, vv) in vm.borrow().entries.iter() {
                        vars.push((vk.v().as_str().unwrap_or("").to_string(), vv.clone()));
                    }
                }
            }
            Some("failed") => failed = x.v().as_str().map(String::from),
            _ => {}
        }
    }
    Some((vars, failed))
}

fn save_state(p: &Pipeline) {
    let _ = std::fs::create_dir_all(STATE_DIR);
    let text = {
        let run = RUN.lock().unwrap();
        let Some(r) = run.as_ref() else { return };
        let vars = if r.vars.is_empty() {
            "{}".to_string()
        } else {
            let items: Vec<String> =
                r.vars.iter().map(|(k, v)| format!("    {}: {}", crate::data::json_string(k), crate::data::to_json(v.v(), 2))).collect();
            format!("{{\n{}\n  }}", items.join(",\n"))
        };
        let mut text = format!("{{\n  \"pipeline\": {},\n  \"vars\": {vars}", crate::data::json_string(&p.name));
        if let Some(f) = &r.failed {
            text.push_str(&format!(",\n  \"failed\": {}", crate::data::json_string(f)));
        }
        text.push_str("\n}\n");
        text
    };
    let _ = std::fs::write(state_path(p), text);
}

fn call_step(f: &Val, p: &Pipeline) -> bool {
    *ACTIVE.lock().unwrap() += 1;
    let run = native(Run { pipeline: p.name.clone() });
    let ok = match call_value(f.v(), &[run.v()], 0) {
        Ok(_) => true,
        Err(_) => {
            crate::state::report_pending();
            false
        }
    };
    *ACTIVE.lock().unwrap() -= 1;
    crate::state::flush();
    ok
}

fn set(name: &str, value: Val) {
    if let Some(r) = RUN.lock().unwrap().as_mut() {
        set_var(&mut r.vars, name, value);
    }
}

fn unknown(what: &str, name: &str, names: &[String]) -> crate::state::Fail {
    let d = match suggest(name, names.iter().map(|s| s.as_str())) {
        Some(s) => Diagnostic::new_spanless("ci_unknown").fix("suggest").arg("what", what).arg("name", name).arg("suggestion", s),
        None => Diagnostic::new_spanless("ci_unknown").arg("what", what).arg("name", name),
    };
    err(d, 0)
}

fn pick(name: Option<&str>) -> R<&'static Pipeline> {
    let all = PIPELINES.lock().unwrap().clone();
    if all.is_empty() {
        let file = crate::state::program().file;
        return Err(err(Diagnostic::new_spanless("ci_none").arg("file", file), 0));
    }
    match name {
        None if all.len() > 1 => {
            let names: Vec<&str> = all.iter().map(|p| p.name.as_str()).collect();
            Err(err(Diagnostic::new_spanless("ci_which").arg("names", names.join(", ")), 0))
        }
        None => Ok(all[0]),
        Some(n) => match all.iter().find(|p| p.name == n) {
            Some(p) => Ok(p),
            None => Err(unknown("pipeline", n, &all.iter().map(|p| p.name.clone()).collect::<Vec<_>>())),
        },
    }
}

fn after_fn(p: &Pipeline, what: &str) -> Option<Val> {
    p.after.iter().find(|(w, _)| w == what).map(|(_, f)| f.clone())
}

fn run_pipeline(p: &'static Pipeline, step: Option<&str>, after: Option<&str>, yes: bool) -> R<i32> {
    let missing: Vec<&str> = p.secrets.iter().filter(|s| std::env::var_os(s).is_none()).map(|s| s.as_str()).collect();
    if !missing.is_empty() {
        return Err(err(Diagnostic::new_spanless("ci_secret").arg("names", missing.join(", ")).arg("pipeline", p.name.clone()), 0));
    }
    if let Some(s) = step {
        if !p.steps.iter().any(|(n, _)| n == s) {
            return Err(unknown("step", s, &p.steps.iter().map(|(n, _)| n.clone()).collect::<Vec<_>>()));
        }
    }
    let fresh = || -> R<RunState> { Ok(RunState { vars: vec![("version".into(), Val::str(version_of(p)?)), ("step".into(), Val::str(""))], yes, failed: None }) };
    let state = if step.is_none() && after.is_none() {
        fresh()?
    } else {
        match load_state(p) {
            Some((vars, failed)) => RunState { vars, yes, failed },
            None => fresh()?,
        }
    };
    let old = RUN.lock().unwrap().replace(state);
    drop(old);
    if let Some(what) = after {
        if let Some(f) = after_fn(p, what) {
            if what == "failure" {
                let failed = RUN.lock().unwrap().as_ref().and_then(|r| r.failed.clone()).unwrap_or_default();
                set("step", Val::str(failed));
            }
            say(&m("info.ci_after", &[("what", what.to_string())]));
            if !call_step(&f, p) {
                return Ok(1);
            }
        }
        return Ok(0);
    }
    let steps: Vec<&(String, Val)> = p.steps.iter().filter(|(n, _)| step.is_none_or(|s| s == n)).collect();
    for (name, f) in &steps {
        set("step", Val::str(name.clone()));
        say(&m("info.ci_step", &[("step", name.clone())]));
        if !call_step(f, p) {
            if let Some(r) = RUN.lock().unwrap().as_mut() {
                r.failed = Some(name.clone());
            }
            save_state(p);
            say(&m("info.ci_failed", &[("pipeline", p.name.clone()), ("step", name.clone())]));
            if step.is_none() {
                if let Some(f) = after_fn(p, "failure") {
                    say(&m("info.ci_after", &[("what", "failure".into())]));
                    call_step(&f, p);
                }
            }
            return Ok(1);
        }
        save_state(p);
    }
    if step.is_none() {
        if let Some(f) = after_fn(p, "success") {
            set("step", Val::str(""));
            say(&m("info.ci_after", &[("what", "success".into())]));
            if !call_step(&f, p) {
                return Ok(1);
            }
        }
        let version = var_text("version");
        say(&m("info.ci_done", &[("pipeline", p.name.clone()), ("n", steps.len().to_string()), ("version", version)]));
    }
    Ok(0)
}

fn command() -> R<i32> {
    let env = |k: &str| std::env::var(k).ok().filter(|s| !s.is_empty());
    let name = env("NEXO_CI_PIPELINE");
    let p = pick(name.as_deref())?;
    if let Some(target) = env("NEXO_CI_EXPORT") {
        let mut approvals = Vec::new();
        for line in env("NEXO_CI_APPROVALS").unwrap_or_default().lines() {
            let mut parts = line.split('\x1f').map(String::from);
            if let Some(step) = parts.next() {
                approvals.push((step, parts.collect::<Vec<_>>()));
            }
        }
        let file = crate::state::program().file;
        let (path, text) = crate::ci_export::export(&target, p, file, &approvals);
        if let Some(dir) = std::path::Path::new(&path).parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        if let Err(e) = std::fs::write(&path, text) {
            return Err(crate::data::os_failure(&path, &e, 0));
        }
        say(&m("info.ci_exported", &[("path", path)]));
        return Ok(0);
    }
    run_pipeline(p, env("NEXO_CI_STEP").as_deref(), env("NEXO_CI_AFTER").as_deref(), env("NEXO_CI_YES").is_some())
}

/// `nexo ci run|export`: the driver compiled the program's declarations
/// and blocks; the command comes in the environment.
pub fn run_command() -> i32 {
    crate::state::flush();
    match command() {
        Ok(code) => {
            crate::state::flush();
            code
        }
        Err(_) => {
            crate::state::report_pending();
            1
        }
    }
}
