//! Read-only configured ceilings, through the same loaders/engines as Shield.
use crate::json::{obj, s, Json};
use nexo_diag::Diagnostic;
use nexo_runtime::security::{authorization, context, Capability};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

pub(crate) struct Configuration {
    root: PathBuf,
    dependencies: Option<context::Configuration>,
    authorization: Option<authorization::Configuration>,
}
impl Configuration {
    pub fn load(root: &str, deps: Option<&str>, auth: Option<&str>) -> Result<Self, Diagnostic> {
        let dep = deps
            .map(String::from)
            .or_else(|| std::env::var("NEXO_SECURITY_DEPENDENCIES").ok());
        let auth = auth
            .map(String::from)
            .or_else(|| std::env::var("NEXO_SECURITY_AUTHORIZATION").ok());
        let dependencies = dep
            .map(|p| context::Configuration::load(Path::new(&p), Path::new(root)))
            .transpose()
            .map_err(|_| Diagnostic::new_spanless("security_context_config"))?;
        let authorization = auth
            .map(|p| authorization::Configuration::load(Path::new(&p)))
            .transpose()
            .map_err(|_| Diagnostic::new_spanless("security_authorization"))?;
        Ok(Self {
            root: Path::new(root)
                .canonicalize()
                .unwrap_or_else(|_| PathBuf::from(root)),
            dependencies,
            authorization,
        })
    }
    pub fn has_authorization(&self) -> bool {
        self.authorization.is_some()
    }
    pub fn active(&self) -> bool {
        self.dependencies.is_some() || self.authorization.is_some()
    }
    pub fn approval(&self, file: &str, symbol: &str) -> bool {
        let Some(auth) = &self.authorization else {
            return false;
        };
        let file = Path::new(file)
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(file));
        auth.analysis_confirmation(&file, symbol)
    }
    pub fn registrations(&self, file: &str, symbol: &str) -> Vec<(&'static str, PathBuf, String)> {
        let Some(auth) = &self.authorization else {
            return vec![];
        };
        let file = Path::new(file)
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(file));
        auth.analysis_registrations(&file, symbol)
    }
    pub fn contexts(&self, file: &str, symbol: &str) -> BTreeSet<(String, String)> {
        if !self.active() {
            return BTreeSet::new();
        }
        let identity = Path::new(file)
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(file));
        let mut out = BTreeSet::new();
        if self.dependencies.is_some() && identity != self.root {
            out.insert((file.into(), String::new()));
        }
        if self
            .authorization
            .as_ref()
            .is_some_and(|a| a.analysis_has_tool(&identity, symbol))
        {
            out.insert((file.into(), symbol.into()));
            if let Some(auth) = &self.authorization {
                for agent in auth.analysis_agent_files(&identity, symbol) {
                    out.insert((crate::modules::shown_path(&agent), "#registration".into()));
                }
            }
        }
        out
    }
    pub fn checks(
        &self,
        contexts: &BTreeSet<(String, String)>,
        kind: &str,
        target: &str,
    ) -> Vec<Json> {
        let Some(cap) = Capability::from_boundary(kind, target) else {
            return vec![];
        };
        let mut out = BTreeMap::new();
        for (file, symbol) in contexts {
            let file = Path::new(file)
                .canonicalize()
                .unwrap_or_else(|_| PathBuf::from(file));
            if let Some(deps) = &self.dependencies {
                if file != self.root {
                    let name = crate::modules::shown_path(&file);
                    out.insert(
                        ("dependency".to_string(), name.clone()),
                        row("dependency", &name, deps.analysis_allows(&file, cap), false),
                    );
                }
            }
            if let Some(auth) = &self.authorization {
                for d in auth.analysis_checks(&file, symbol, cap) {
                    let name = format!("{}::{}", crate::modules::shown_path(&d.file), d.symbol);
                    out.insert(
                        (d.scope.to_string(), name.clone()),
                        row(d.scope, &name, d.allowed, d.confirm),
                    );
                }
            }
        }
        out.into_values().collect()
    }
}
fn row(scope: &str, identity: &str, allowed: bool, approval: bool) -> Json {
    obj(vec![
        ("scope", s(scope)),
        ("identity", s(identity)),
        ("status", s(if allowed { "allowed" } else { "denied" })),
        ("approval_required", Json::Bool(approval)),
    ])
}
