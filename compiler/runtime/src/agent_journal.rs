//! D151 recovery semantics applied to a confined agent dispatch journal.
use crate::{
    agent_state::{digest, Lock, Store},
    json::{obj, s, Json},
    state::R,
};
use std::sync::Arc;
pub type SavedCall = (String, String, String);
pub struct Frame {
    pub tokens: Json,
    pub phase: String,
    pub step: i64,
    pub messages: Vec<String>,
    pub calls: Vec<SavedCall>,
    pub results: Vec<String>,
    pub text: String,
    pub tool_turn: bool,
}
pub struct Journal {
    pub store: Arc<Store>,
    _lock: Lock,
    name: String,
    identity: String,
    target: String,
    started: bool,
    pub frame: Option<Frame>,
}
fn bad(span: u32) -> crate::state::Fail {
    crate::security::authorization::state_error(span)
}
fn strings(j: &Json) -> Option<Vec<String>> {
    let Json::Arr(v) = j else { return None };
    v.iter().map(|v| v.str().map(str::to_string)).collect()
}
impl Journal {
    pub fn open(
        seal: &crate::security::authorization::Seal,
        run: &str,
        input: Option<&str>,
        output: &str,
        steps: i64,
        span: u32,
    ) -> R<Self> {
        if run.is_empty() || run.len() > 256 {
            return Err(bad(span));
        }
        let (grant, config, agent) = crate::security::authorization::state_grant(seal, span)?;
        let store = Arc::new(Store::open(&grant).map_err(|_| bad(span))?);
        let mut sources = std::collections::BTreeMap::new();
        for (id, file) in nexo_diag::registered_files() {
            if let Some((_, text)) = nexo_diag::file_of(id) {
                sources.insert(
                    std::path::Path::new(file)
                        .canonicalize()
                        .map_err(|_| bad(span))?,
                    text.to_string(),
                );
            }
        }
        let p = crate::state::program();
        sources.insert(
            std::path::Path::new(p.file)
                .canonicalize()
                .map_err(|_| bad(span))?,
            p.source.to_string(),
        );
        let source = Json::Arr(
            sources
                .iter()
                .map(|(file, text)| Json::Arr(vec![s(file.display().to_string()), s(digest(text))]))
                .collect(),
        );
        let identity = digest(
            &Json::Arr(vec![
                s("native"),
                s(crate::security::authorization::context_identity(seal)),
                s(config),
                source,
                s(agent.clone()),
                s(run),
                input.map(s).unwrap_or(Json::Null),
                s(output),
            ])
            .compact(),
        );
        let name = digest(&Json::Arr(vec![s(agent.clone()), s(run)]).compact());
        let lock = store.lock(&name).map_err(|_| bad(span))?;
        let saved = store.read(&name).map_err(|_| bad(span))?;
        let mut target = String::new();
        let frame = if let Some(j) = saved {
            let mut decode = || -> Option<Frame> {
                let Json::Obj(fields) = &j else { return None };
                if fields.len() != 11
                    || j.get("version").int() != Some(1)
                    || j.get("identity").str() != Some(&identity)
                {
                    return None;
                }
                target = j.get("target").str()?.to_string();
                let phase = j.get("phase").str()?.to_string();
                if !["ready", "response", "approval", "complete"].contains(&phase.as_str()) {
                    return None;
                }
                let step = j.get("step").int().filter(|n| *n >= 0 && *n < steps)?;
                let messages = strings(j.get("messages"))?;
                let results = strings(j.get("results"))?;
                for text in messages.iter().chain(results.iter()) {
                    crate::json::parse_unique(text)?;
                }
                let Json::Arr(cs) = j.get("calls") else {
                    return None;
                };
                let mut calls = vec![];
                for c in cs {
                    let words = strings(c)?;
                    if words.len() != 3
                        || !matches!(crate::json::parse_unique(&words[2])?, Json::Obj(_))
                    {
                        return None;
                    }
                    calls.push((words[0].clone(), words[1].clone(), words[2].clone()));
                }
                let tool_turn = j.get("tool_turn").bool()?;
                let text = j.get("text").str()?.to_string();
                let ids = calls
                    .iter()
                    .map(|c| &c.0)
                    .collect::<std::collections::HashSet<_>>();
                if ids.len() != calls.len()
                    || (!tool_turn && (!calls.is_empty() || !results.is_empty()))
                    || (["complete", "ready"].contains(&phase.as_str())
                        && (tool_turn || !calls.is_empty() || !results.is_empty()))
                    || (phase == "ready" && !text.is_empty())
                    || (phase == "approval" && (!tool_turn || calls.is_empty()))
                {
                    return None;
                }
                Some(Frame {
                    tokens: j.get("tokens").clone(),
                    phase,
                    step,
                    messages,
                    calls,
                    results,
                    text: j.get("text").str()?.into(),
                    tool_turn: j.get("tool_turn").bool()?,
                })
            };
            Some(decode().ok_or_else(|| bad(span))?)
        } else {
            None
        };
        Ok(Self {
            store,
            _lock: lock,
            name,
            identity,
            target,
            started: false,
            frame,
        })
    }
    pub fn bind(&mut self, target: &str, span: u32) -> R<()> {
        let id = digest(target);
        if self.frame.is_some() && self.target != id {
            return Err(bad(span));
        }
        if let Some(f) = &self.frame {
            crate::agent_tokens::restore(&f.tokens, span)?;
        }
        crate::agent_tokens::attach(
            self.store.clone(),
            &self.name,
            &format!("{}\n{}", self.identity, id),
            self.frame.is_none(),
            span,
        )?;
        self.target = id;
        Ok(())
    }
    pub fn save(&mut self, frame: Frame, span: u32) -> R<()> {
        self.frame = Some(frame);
        self.flush(span)?;
        self.started = true;
        Ok(())
    }
    fn flush(&self, span: u32) -> R<()> {
        let Some(f) = &self.frame else { return Ok(()) };
        let strings = |v: &[String]| Json::Arr(v.iter().map(|s_| s(s_.clone())).collect());
        let calls = Json::Arr(
            f.calls
                .iter()
                .map(|(id, name, args)| {
                    Json::Arr(vec![s(id.clone()), s(name.clone()), s(args.clone())])
                })
                .collect(),
        );
        let value = obj(vec![
            ("tokens", f.tokens.clone()),
            ("version", Json::Int(1)),
            ("identity", s(self.identity.clone())),
            ("target", s(self.target.clone())),
            ("phase", s(f.phase.clone())),
            ("step", Json::Int(f.step)),
            ("messages", strings(&f.messages)),
            ("calls", calls),
            ("results", strings(&f.results)),
            ("text", s(f.text.clone())),
            ("tool_turn", Json::Bool(f.tool_turn)),
        ]);
        self.store.write(&self.name, &value).map_err(|_| bad(span))
    }
    pub fn phase(&mut self, phase: &str, span: u32) -> R<()> {
        if let Some(f) = &mut self.frame {
            f.phase = phase.into()
        }
        self.flush(span)?;
        self.started = true;
        Ok(())
    }
    pub fn fail(&mut self, span: u32) -> R<()> {
        if self.started && self.frame.as_ref().is_none_or(|f| f.phase != "complete") {
            self.phase("failed", span)
        } else {
            Ok(())
        }
    }
}
