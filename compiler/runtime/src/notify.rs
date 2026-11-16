//! nexo.notify (section 15, D209): `send_email(to, text, subject:)`,
//! `slack(channel, text)` and `notify(to, text)`, which picks one of the
//! two by what `to` looks like. Mail is handed over to the SMTP server of
//! the `[notify]` table of the project's nexo.toml, spoken here with no
//! library (RFC 5321, AUTH PLAIN, a text/plain message in base64); Slack
//! takes the message through its Web API with the token of an app. Under
//! `nexo test` nothing is sent: each message is a line of the test's
//! output.
use std::path::Path;

use nexo_diag::{msg, Diagnostic};

use crate::data::Json;
use crate::kafka::{local_host, open, Stream};
use crate::state::{diag, failure, failure_as, R};
use crate::stdlib::expects;
use crate::value::*;

const SLACK: &str = "https://slack.com/api";
/// 1970-01-01 was a Thursday
const DAY_NAMES: [&str; 7] = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"];
const MONTH_NAMES: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const NOT_IN_ADDRESS: &str = "<>(),;:\"[]\\";

// ------------------------------------------------------------ settings

/// The `[notify]` table of the nexo.toml of the program's folder or the
/// nearest one above: lines `name = "text"`.
fn settings(span: u32) -> R<Vec<(String, String)>> {
    if crate::security::enforcing() {
        return Ok(Vec::new());
    }
    let file = crate::state::program().file;
    let start = std::fs::canonicalize(file)
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .or_else(|| std::env::current_dir().ok())
        .unwrap_or_default();
    let mut dir = Some(start.as_path());
    while let Some(d) = dir {
        let path = d.join("nexo.toml");
        if path.is_file() {
            return read_table(&path).map_err(|d| diag(d, span));
        }
        dir = d.parent();
    }
    Ok(Vec::new())
}

fn read_table(path: &Path) -> Result<Vec<(String, String)>, Diagnostic> {
    let mut table: Vec<(String, String)> = Vec::new();
    let Ok(text) = std::fs::read_to_string(path) else { return Ok(table) };
    let mut inside = false;
    for (n, raw) in text.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') {
            inside = line.split('#').next().unwrap_or("").trim().trim_matches(['[', ']']).trim() == "notify";
            continue;
        }
        if !inside {
            continue;
        }
        let (key, value) = line.split_once('=').unwrap_or((line, ""));
        let parsed = if line.contains('=') { crate::ai::toml_value(value.trim()) } else { None };
        match parsed {
            Some(Json::Str(s)) if !key.trim().is_empty() => {
                table.retain(|(k, _)| k != key.trim());
                table.push((key.trim().to_string(), s));
            }
            _ => return Err(Diagnostic::new_spanless("ai_toml").arg("path", path.display().to_string()).arg("line", (n + 1).to_string())),
        }
    }
    Ok(table)
}

fn setting(table: &[(String, String)], key: &str) -> String {
    table.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone()).unwrap_or_default()
}

fn invalid(detail: &str, args: &[(&str, String)], span: u32) -> crate::state::Fail {
    failure("InvalidValue", span, &[("detail", msg(&format!("detail.{detail}"), args))])
}

fn text_arg(fname: &str, v: V, span: u32) -> R<String> {
    v.as_str().map(|s| s.to_string()).ok_or_else(|| expects(fname, "str", v, span))
}

fn is_address(t: &str) -> bool {
    crate::stdlib::is_email(t) && t.chars().all(|c| (33..127).contains(&(c as u32)) && !NOT_IN_ADDRESS.contains(c))
}

// ---------------------------------------------------------------- mail

/// Mon, 16 Nov 2026 10:00:00 +0000
fn mail_date(seconds: i64) -> String {
    let (days, rest) = (seconds.div_euclid(86400), seconds.rem_euclid(86400));
    let (y, m, d) = crate::time::civil_from_days(days);
    format!(
        "{}, {:02} {} {} {:02}:{:02}:{:02} +0000",
        DAY_NAMES[days.rem_euclid(7) as usize],
        d,
        MONTH_NAMES[(m - 1) as usize],
        y,
        rest / 3600,
        rest / 60 % 60,
        rest % 60
    )
}

/// A header's value: as it is when it is plain ASCII, else as encoded
/// words of at most 39 bytes each (RFC 2047), so a line stays under 76.
fn header_text(text: &str) -> String {
    if text.chars().all(|c| (32..127).contains(&(c as u32))) {
        return text.to_string();
    }
    let mut words: Vec<Vec<u8>> = Vec::new();
    let mut chunk: Vec<u8> = Vec::new();
    for c in text.chars() {
        let mut b = [0u8; 4];
        let piece = c.encode_utf8(&mut b).as_bytes();
        if chunk.len() + piece.len() > 39 {
            words.push(std::mem::take(&mut chunk));
        }
        chunk.extend_from_slice(piece);
    }
    words.push(chunk);
    words.iter().map(|w| format!("=?utf-8?B?{}?=", crate::crypto::b64(w))).collect::<Vec<_>>().join("\r\n ")
}

fn domain(address: &str) -> &str {
    address.rsplit('@').next().unwrap_or("")
}

fn message(sender: &str, to: &str, subject: &str, text: &str, seconds: i64, ident: &str) -> Vec<u8> {
    let body = crate::crypto::b64(text.as_bytes());
    let mut lines = vec![
        format!("From: {sender}"),
        format!("To: {to}"),
        format!("Subject: {}", header_text(subject)),
        format!("Date: {}", mail_date(seconds)),
        format!("Message-ID: <{ident}@{}>", domain(sender)),
        "MIME-Version: 1.0".to_string(),
        "Content-Type: text/plain; charset=utf-8".to_string(),
        "Content-Transfer-Encoding: base64".to_string(),
        String::new(),
    ];
    for part in body.as_bytes().chunks(76) {
        lines.push(String::from_utf8_lossy(part).into_owned());
    }
    (lines.join("\r\n") + "\r\n.\r\n").into_bytes()
}

/// (tls, user, host, port) of smtps://user@host:port.
fn server_of(url: &str) -> Option<(bool, String, String, u16)> {
    let (scheme, rest) = url.split_once("://")?;
    if !(scheme == "smtp" || scheme == "smtps") || rest.is_empty() || rest.contains('/') {
        return None;
    }
    let (user, hostport) = rest.rsplit_once('@').unwrap_or(("", rest));
    let (host, port) = hostport.split_once(':').unwrap_or((hostport, ""));
    if host.is_empty() || user.contains(':') || !port.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let tls = scheme == "smtps";
    let port = if port.is_empty() { if tls { 465 } else { 25 } } else { port.parse().ok()? };
    Some((tls, user.to_string(), host.to_lowercase(), port))
}

struct Line {
    stream: Stream,
    buf: Vec<u8>,
}

impl Line {
    /// The code and the last line of a reply.
    fn reply(&mut self) -> Result<(String, String), String> {
        loop {
            while !self.buf.contains(&b'\n') {
                let mut chunk = [0u8; 4096];
                match self.stream.read(&mut chunk) {
                    Ok(0) => return Err("the server closed the connection".into()),
                    Ok(n) => self.buf.extend_from_slice(&chunk[..n]),
                    Err(_) => return Err("connection lost".into()),
                }
            }
            let at = self.buf.iter().position(|b| *b == b'\n').unwrap();
            let line: Vec<u8> = self.buf.drain(..=at).collect();
            let text = String::from_utf8_lossy(&line[..line.len() - 1]).trim_end_matches('\r').to_string();
            if text.len() < 4 || text.as_bytes()[3] != b'-' {
                return Ok((text.chars().take(3).collect(), text));
            }
        }
    }

    fn say(&mut self, line: &[u8], good: &[&str]) -> Result<(), String> {
        if !line.is_empty() && self.stream.write_all(line).is_err() {
            return Err("connection lost".into());
        }
        let (code, text) = self.reply()?;
        if good.contains(&code.as_str()) {
            Ok(())
        } else {
            Err(format!("it answered {text}"))
        }
    }
}

/// One message, handed over to the server (RFC 5321).
#[allow(clippy::too_many_arguments)]
fn deliver(tls: bool, user: &str, password: &str, host: &str, port: u16, sender: &str, to: &str, data: &[u8]) -> Result<(), String> {
    if !tls && !local_host(host) {
        return Err(format!("{host} is reached with smtps:// only"));
    }
    let mut c = Line { stream: open(host, port, tls)?, buf: Vec::new() };
    c.say(b"", &["220"])?;
    c.say(format!("EHLO {}\r\n", domain(sender)).as_bytes(), &["250"])?;
    if !user.is_empty() {
        let token = crate::crypto::b64(format!("\0{user}\0{password}").as_bytes());
        c.say(format!("AUTH PLAIN {token}\r\n").as_bytes(), &["235"])?;
    }
    c.say(format!("MAIL FROM:<{sender}>\r\n").as_bytes(), &["250"])?;
    c.say(format!("RCPT TO:<{to}>\r\n").as_bytes(), &["250", "251"])?;
    c.say(b"DATA\r\n", &["354"])?;
    c.say(data, &["250"])?;
    let _ = c.stream.write_all(b"QUIT\r\n");
    Ok(())
}

/// `send_email(to, text, subject:)`
pub fn send_email(to: V, text: V, subject: Option<V>, span: u32) -> R<Val> {
    let to = text_arg("send_email", to, span)?;
    let text = text_arg("send_email", text, span)?;
    let subject = match subject.filter(|s| !s.is_none() && s.tag != T_MISSING) {
        Some(s) => text_arg("send_email", s, span)?,
        None => text.split('\n').next().unwrap_or("").trim().to_string(),
    };
    let subject = subject.split_whitespace().collect::<Vec<_>>().join(" ");
    if !is_address(&to) {
        return Err(invalid("notify_address", &[("to", to)], span));
    }
    if crate::stream::in_test_mode() {
        crate::state::write_out(&format!("{}\n", msg("info.notify_email", &[("to", to), ("subject", subject)])));
        return Ok(Val::none());
    }
    let table = settings(span)?;
    let (url, sender) = (setting(&table, "smtp"), setting(&table, "from"));
    if url.is_empty() || sender.is_empty() {
        return Err(failure_as("NetworkError", "NetworkError.mail_setup", span, &[("to", to)]));
    }
    let Some((tls, user, host, port)) = server_of(&url) else {
        return Err(invalid("notify_smtp", &[("url", url)], span));
    };
    if !is_address(&sender) {
        return Err(invalid("notify_address", &[("to", sender)], span));
    }
    crate::policy::check("net", &host, span)?;
    let key = setting(&table, "key");
    let password = if !user.is_empty() && !key.is_empty() { crate::crypto::Secret { name: key }.reveal(span)? } else { String::new() };
    let now = crate::time::now_ms();
    let ident = format!("{now}.{}", crate::crypto::random(4).iter().map(|b| format!("{b:02x}")).collect::<String>());
    let data = message(&sender, &to, &subject, &text, now / 1000, &ident);
    match deliver(tls, &user, &password, &host, port, &sender, &to, &data) {
        Ok(()) => Ok(Val::none()),
        Err(reason) => Err(failure_as("NetworkError", "NetworkError.mail", span, &[("server", host), ("to", to), ("reason", reason)])),
    }
}

// --------------------------------------------------------------- slack

/// `slack(channel, text)`
pub fn slack(channel: V, text: V, span: u32) -> R<Val> {
    let channel = text_arg("slack", channel, span)?;
    let text = text_arg("slack", text, span)?;
    if channel.trim().is_empty() || channel.chars().any(|c| c.is_whitespace()) {
        return Err(invalid("notify_channel", &[("channel", channel)], span));
    }
    if crate::stream::in_test_mode() {
        crate::state::write_out(&format!("{}\n", msg("info.notify_slack", &[("channel", channel), ("text", text)])));
        return Ok(Val::none());
    }
    let name = setting(&settings(span)?, "slack");
    if name.is_empty() {
        return Err(failure_as("NetworkError", "NetworkError.slack_setup", span, &[("channel", channel)]));
    }
    let token = crate::crypto::Secret { name }.reveal(span)?;
    let base = std::env::var("NEXO_SLACK_URL").ok().filter(|u| !u.is_empty()).unwrap_or_else(|| SLACK.to_string());
    let body = format!("{{\"channel\":{},\"text\":{}}}", crate::data::json_string(&channel), crate::data::json_string(&text));
    let url = format!("{}/chat.postMessage", base.trim_end_matches('/'));
    let headers = [("Authorization".to_string(), format!("Bearer {token}"))];
    let r = crate::http::send("POST", &url, &Some((body.into_bytes(), "application/json; charset=utf-8")), &headers, 30.0, 1, span)?;
    let answer = crate::data::parse_json(&r.text()).ok();
    let field = |key: &str| match &answer {
        Some(Json::Obj(pairs)) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
        _ => None,
    };
    if matches!(field("ok"), Some(Json::Bool(true))) {
        return Ok(Val::none());
    }
    let reason = match field("error") {
        Some(Json::Str(e)) => e.clone(),
        _ => "no answer".to_string(),
    };
    Err(failure_as("NetworkError", "NetworkError.slack", span, &[("channel", channel), ("reason", reason)]))
}

/// `notify(to, text)`: an address gets a mail, a #channel a message of
/// Slack.
pub fn notify(to: V, text: V, span: u32) -> R<Val> {
    let name = text_arg("notify", to, span)?;
    text_arg("notify", text, span)?;
    if name.starts_with('#') {
        return slack(to, text, span);
    }
    if is_address(&name) {
        return send_email(to, text, None, span);
    }
    Err(invalid("notify_to", &[("to", name)], span))
}
