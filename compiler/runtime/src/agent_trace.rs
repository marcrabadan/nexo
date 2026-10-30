//! Explicit stderr-only OTLP JSON. No prompts, identities or error messages.
use crate::{
    json::{obj, s, Json},
    state::{diag, R},
};
use std::{
    cell::RefCell,
    io::Write,
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};
thread_local! { static CURRENT: RefCell<Option<(String,String)>> = const { RefCell::new(None) }; }
static WRITE: Mutex<()> = Mutex::new(());
fn now() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
}
fn random<const N: usize>() -> Result<String, ()> {
    let mut bytes = [0; N];
    getrandom::getrandom(&mut bytes).map_err(|_| ())?;
    if bytes.iter().all(|b| *b == 0) {
        bytes[N - 1] = 1;
    }
    Ok(bytes.iter().map(|x| format!("{x:02x}")).collect())
}
fn invalid(span: u32) -> crate::state::Fail {
    diag(nexo_diag::Diagnostic::new_spanless("agent_trace"), span)
}
pub fn traced<T>(name: &'static str, span: u32, f: impl FnOnce() -> R<T>) -> R<T> {
    match std::env::var("NEXO_AGENT_TRACE") {
        Err(std::env::VarError::NotPresent) => return f(),
        Ok(v) if v.is_empty() => return f(),
        Ok(v) if v == "stderr" => (),
        _ => return Err(invalid(span)),
    }
    let parent = CURRENT.with(|p| p.borrow().clone());
    let trace = match &parent {
        Some(p) => p.0.clone(),
        None => random::<16>().map_err(|_| invalid(span))?,
    };
    let id = random::<8>().map_err(|_| invalid(span))?;
    let start = now();
    CURRENT.with(|p| *p.borrow_mut() = Some((trace.clone(), id.clone())));
    let result = f();
    CURRENT.with(|p| *p.borrow_mut() = parent.clone());
    let mut data = obj(vec![
        ("traceId", s(trace)),
        ("spanId", s(id)),
        ("name", s(name)),
        ("kind", Json::Int(1)),
        ("startTimeUnixNano", s(start.to_string())),
        ("endTimeUnixNano", s(now().max(start).to_string())),
        (
            "status",
            obj(vec![(
                "code",
                Json::Int(if result.is_ok() { 1 } else { 2 }),
            )]),
        ),
    ]);
    if let Some(p) = parent {
        data.set("parentSpanId", s(p.1));
    }
    let envelope = obj(vec![(
        "resourceSpans",
        Json::Arr(vec![obj(vec![
            (
                "resource",
                obj(vec![(
                    "attributes",
                    Json::Arr(vec![obj(vec![
                        ("key", s("service.name")),
                        ("value", obj(vec![("stringValue", s("nexo"))])),
                    ])]),
                )]),
            ),
            (
                "scopeSpans",
                Json::Arr(vec![obj(vec![
                    (
                        "scope",
                        obj(vec![("name", s("nexo.agent")), ("version", s("1"))]),
                    ),
                    ("spans", Json::Arr(vec![data])),
                ])]),
            ),
        ])]),
    )]);
    let _lock = WRITE.lock().unwrap();
    writeln!(std::io::stderr().lock(), "{}", envelope.compact()).map_err(|_| invalid(span))?;
    result
}
