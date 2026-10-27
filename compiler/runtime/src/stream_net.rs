//! nexo.stream on brokers on the network (section 19, D169), as phase 0's
//! stream_net.py: Kafka (kafka.rs), Azure Event Hubs through its Kafka
//! endpoint, RabbitMQ (amqp.rs) and WebSocket servers (websocket.rs).

use std::collections::HashMap;
use std::sync::Mutex;

use crate::state::{failure_as, R};
use crate::stream::{kept, Id, Message};
use crate::value::*;

type Pair = (String, String);

fn ident(s: &Pair, place: &str) -> String {
    format!("{}:{}@{place}", s.0, s.1)
}

fn broker_failure(s: &Pair, reason: String, span: u32) -> crate::state::Fail {
    failure_as("NetworkError", "NetworkError.broker", span, &[("stream", format!("{}(\"{}\")", s.0, s.1)), ("reason", reason)])
}

/// key: of the sources, by stream: the password of their broker.
static SECRETS: Mutex<Vec<(String, Val)>> = Mutex::new(Vec::new());

pub fn keep_secret(s: &Pair, place: &str, secret: V) {
    let id = ident(s, place);
    let mut all = SECRETS.lock().unwrap();
    all.retain(|(k, _)| *k != id);
    all.push((id, Val::from(secret)));
}

fn password(s: &Pair, place: &str, span: u32) -> R<Option<String>> {
    let found = SECRETS.lock().unwrap().iter().find(|(k, _)| *k == ident(s, place)).map(|(_, v)| v.clone());
    match found {
        Some(v) => Ok(Some(crate::crypto::text_or_secret("key", v.v(), span)?)),
        None => Ok(None),
    }
}

/// (user, [(host, port)]) of `user@host:9092,host2:9092`.
fn servers_of(text: &str) -> (Option<String>, Vec<(String, u16)>) {
    let mut user = None;
    let mut out = Vec::new();
    for part in text.split(',') {
        let mut part = part.trim();
        if let Some((u, rest)) = part.rsplit_once('@') {
            user = Some(u.to_string());
            part = rest;
        }
        let (host, port) = match part.rsplit_once(':') {
            Some((h, p)) if !h.is_empty() => (h, p.parse().unwrap_or(9092)),
            _ => (part, 9092),
        };
        out.push((host.trim_matches(['[', ']']).to_string(), port));
    }
    (user, out)
}

fn eventhub_address(namespace: &str) -> String {
    std::env::var("NEXO_EVENTHUB_SERVERS").ok().filter(|v| !v.is_empty()).unwrap_or(format!("{namespace}.servicebus.windows.net:9093"))
}

/// The host a source talks to (for the policy's `net`).
pub fn host(kind: &str, place: &str) -> String {
    match kind {
        "kafka" => servers_of(place).1.first().map(|x| x.0.clone()).unwrap_or_default(),
        "eventhub" => servers_of(&eventhub_address(place)).1.first().map(|x| x.0.clone()).unwrap_or_default(),
        "rabbit" => crate::amqp::parse_url(place).2,
        _ => crate::http::url_host(&place.replacen("ws", "http", 1)),
    }
}

// ---------------------------------------------------------------- Kafka

static KAFKA: Mutex<Option<HashMap<String, crate::kafka::Client>>> = Mutex::new(None);

fn kafka<T>(s: &Pair, place: &str, span: u32, what: impl FnOnce(&mut crate::kafka::Client) -> Result<T, crate::kafka::KafkaError>) -> R<T> {
    let id = ident(s, place);
    let mut all = KAFKA.lock().unwrap();
    let all = all.get_or_insert_with(HashMap::new);
    if !all.contains_key(&id) {
        let client = if s.0 == "eventhub" {
            let scope = format!("https://{place}.servicebus.windows.net/.default");
            let token = crate::stream_cloud::azure_token(s, &scope, span)?;
            let payload = format!("n,,\x01auth=Bearer {token}\x01\x01").into_bytes();
            crate::kafka::Client::new(servers_of(&eventhub_address(place)).1, Some(("OAUTHBEARER".into(), payload)))
        } else {
            let (user, servers) = servers_of(place);
            let sasl = password(s, place, span)?.map(|p| ("PLAIN".to_string(), format!("\0{}\0{p}", user.unwrap_or_default()).into_bytes()));
            crate::kafka::Client::new(servers, sasl)
        };
        all.insert(id.clone(), client);
    }
    // the policy's failure, when it stopped a connection (D142)
    let blocked = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let flag = blocked.clone();
    let client = all.get_mut(&id).unwrap();
    client.check = Some(Box::new(move |host: &str| {
        let ok = crate::policy::check("net", host, span).is_ok();
        if !ok {
            flag.store(true, std::sync::atomic::Ordering::SeqCst);
        }
        ok
    }));
    match what(client) {
        Ok(v) => Ok(v),
        Err(_) if blocked.load(std::sync::atomic::Ordering::SeqCst) => Err(crate::state::Fail),
        Err(e) => {
            if e.code.is_none() {
                // a connection that is lost is made again next time
                all.remove(&id);
            }
            Err(broker_failure(s, e.reason, span))
        }
    }
}

// -------------------------------------------------------------- RabbitMQ

static RABBIT: Mutex<Option<HashMap<String, crate::amqp::Connection>>> = Mutex::new(None);

fn rabbit<T>(s: &Pair, place: &str, span: u32, what: impl Fn(&mut crate::amqp::Connection) -> Result<T, crate::amqp::AmqpError>) -> R<T> {
    let id = ident(s, place);
    let mut again = true;
    loop {
        let mut all = RABBIT.lock().unwrap();
        let all = all.get_or_insert_with(HashMap::new);
        let fresh = !all.contains_key(&id);
        if fresh {
            let p = password(s, place, span)?;
            match crate::amqp::Connection::open(place, p.as_deref()) {
                Ok(c) => {
                    all.insert(id.clone(), c);
                }
                Err(e) => return Err(broker_failure(s, e.reason, span)),
            }
        }
        match what(all.get_mut(&id).unwrap()) {
            Ok(v) => return Ok(v),
            Err(e) if e.lost => {
                all.remove(&id);
                // a connection the broker closed while it was idle is made
                // again once (D169)
                if again && !fresh {
                    again = false;
                    continue;
                }
                return Err(broker_failure(s, e.reason, span));
            }
            Err(e) => return Err(broker_failure(s, e.reason, span)),
        }
    }
}

fn field_int(headers: &[(String, crate::amqp::Field)], k: &str) -> Option<i64> {
    headers.iter().find(|(n, _)| n == k).and_then(|(_, v)| if let crate::amqp::Field::Int(i) = v { Some(*i) } else { None })
}

fn field_text(headers: &[(String, crate::amqp::Field)], k: &str) -> Option<String> {
    headers.iter().find(|(n, _)| n == k).and_then(|(_, v)| if let crate::amqp::Field::Text(t) = v { Some(t.clone()) } else { None })
}

// ------------------------------------------------------------- WebSocket

static SOCKETS: Mutex<Option<HashMap<String, (crate::websocket::Socket, i64)>>> = Mutex::new(None);

fn socket<T>(s: &Pair, place: &str, span: u32, what: impl FnOnce(&mut crate::websocket::Socket, &mut i64) -> Result<T, String>) -> R<T> {
    let id = ident(s, place);
    let mut all = SOCKETS.lock().unwrap();
    let all = all.get_or_insert_with(HashMap::new);
    if !all.contains_key(&id) {
        let sock = crate::websocket::Socket::open(place).map_err(|e| broker_failure(s, e, span))?;
        let count = 0;
        all.insert(id.clone(), (sock, count));
    }
    let (sock, count) = all.get_mut(&id).unwrap();
    match what(sock, count) {
        Ok(v) => Ok(v),
        Err(e) => {
            all.remove(&id);
            Err(broker_failure(s, e, span))
        }
    }
}

// ----------------------------------------------------------- the calls

fn sent(s: &Pair, place: &str, id: Id, ms: i64, data: &str, key: Option<&str>, partition: i64) -> Message {
    Message {
        stream: s.clone(),
        place: Some(place.to_string()),
        id,
        ms,
        data: data.to_string(),
        key: key.map(|k| k.to_string()),
        partition,
        before: None,
        receipt: None,
    }
}

pub fn partitions(s: &Pair, place: &str, span: u32) -> R<i64> {
    match s.0.as_str() {
        "kafka" | "eventhub" => kafka(s, place, span, |c| c.metadata(&s.1)),
        "rabbit" => {
            rabbit(s, place, span, |c| c.declare(&s.1))?;
            Ok(1)
        }
        _ => {
            socket(s, place, span, |_, _| Ok(()))?;
            Ok(1)
        }
    }
}

/// Where a consumer group starts in a partition (Kafka): the offset before
/// the one it committed, or before the first one for a new group.
pub fn start(s: &Pair, place: &str, partition: i64, span: u32) -> R<Option<String>> {
    if s.0 != "kafka" && s.0 != "eventhub" {
        return Ok(None);
    }
    let group = crate::stream::group_name();
    let at = kafka(s, place, span, |c| {
        let at = c.committed(&group, &s.1, partition as i32)?;
        Ok(if at >= 0 { at } else { c.earliest(&s.1, partition as i32)? } - 1)
    })?;
    Ok(Some(at.to_string()))
}

pub fn send(s: &Pair, place: &str, data: &str, ms: i64, key: Option<&str>, name: Option<&str>, span: u32) -> R<Message> {
    let topic = name.unwrap_or(&s.1).to_string();
    match s.0.as_str() {
        "kafka" | "eventhub" => {
            let (p, offset) = kafka(s, place, span, |c| {
                let count = match c.counts.get(&topic) {
                    Some(n) => *n,
                    None => c.metadata(&topic)?,
                };
                let p = crate::stream::partition_of(key, data, count);
                let record = (key.map(|k| k.as_bytes().to_vec()), data.as_bytes().to_vec(), ms);
                Ok((p, c.produce(&topic, p as i32, &[record])?))
            })?;
            Ok(sent(s, place, Id::Int(offset), ms, data, key, p))
        }
        "rabbit" => {
            let mut headers = vec![(crate::stream::TIME, crate::amqp::Field::Int(ms))];
            if let Some(k) = key {
                headers.push((crate::stream::KEY, crate::amqp::Field::Text(k.to_string())));
            }
            rabbit(s, place, span, |c| c.publish(&topic, data.as_bytes(), &headers))?;
            Ok(sent(s, place, Id::None, ms, data, key, 0))
        }
        _ => {
            socket(s, place, span, |sock, _| sock.send(data))?;
            Ok(sent(s, place, Id::None, ms, data, key, 0))
        }
    }
}

pub fn poll(s: &Pair, place: &str, partition: i64, position: &Option<String>, span: u32) -> R<Vec<Message>> {
    let mut out = Vec::new();
    match s.0.as_str() {
        "kafka" | "eventhub" => {
            let from = position.as_deref().and_then(|p| p.parse::<i64>().ok()).unwrap_or(-1) + 1;
            let records = kafka(s, place, span, |c| c.fetch(&s.1, partition as i32, from, 100))?;
            for (offset, ts, key, value) in records {
                let mut m = sent(
                    s,
                    place,
                    Id::Int(offset),
                    ts,
                    &String::from_utf8_lossy(&value),
                    key.map(|k| String::from_utf8_lossy(&k).into_owned()).as_deref(),
                    partition,
                );
                m.before = Some((offset - 1).to_string());
                out.push(m);
            }
        }
        "rabbit" => {
            for _ in 0..100 {
                let Some((tag, headers, body)) = rabbit(s, place, span, |c| c.get(&s.1))? else { break };
                let ms = field_int(&headers, crate::stream::TIME).unwrap_or_else(crate::stream::now_ms);
                let key = field_text(&headers, crate::stream::KEY);
                let mut m = sent(s, place, Id::Int(tag as i64), ms, &String::from_utf8_lossy(&body), key.as_deref(), 0);
                m.receipt = Some(tag.to_string());
                out.push(m);
            }
        }
        _ => {
            let texts = socket(s, place, span, |sock, count| {
                let got = sock.messages(std::time::Duration::from_millis(200))?;
                let first = *count;
                *count += got.len() as i64;
                Ok((first, got))
            })?;
            let (first, got) = texts;
            for (i, text) in got.into_iter().enumerate() {
                out.push(sent(s, place, Id::Int(first + i as i64 + 1), crate::stream::now_ms(), &text, None, 0));
            }
        }
    }
    Ok(out)
}

pub fn done(s: &Pair, place: &str, partition: i64, messages: &[Message], position: &Option<String>, span: u32) -> R<()> {
    match s.0.as_str() {
        "kafka" | "eventhub" => {
            let at = kept(s, &Some(place.to_string()), partition, position.clone()).and_then(|p| p.parse::<i64>().ok()).unwrap_or(-1);
            let group = crate::stream::group_name();
            kafka(s, place, span, |c| c.commit(&group, &s.1, partition as i32, at + 1))
        }
        "rabbit" => {
            for m in messages {
                let tag: u64 = m.receipt.as_deref().and_then(|t| t.parse().ok()).unwrap_or(0);
                rabbit(s, place, span, |c| c.ack(tag))?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

/// Send a message that failed to its dead letters; the name they go to.
pub fn dead(m: &Message, place: &str) -> R<String> {
    if m.stream.0 == "websocket" {
        // a WebSocket keeps nothing: its dead letters go to the local broker
        let target = crate::websocket::dead_name(place);
        crate::stream::append_local(&("websocket".to_string(), target.clone()), &m.data, m.ms, m.key.clone());
        return Ok(target);
    }
    let target = format!("{}.dead", m.stream.1);
    send(&m.stream, place, &m.data, m.ms, m.key.as_deref(), Some(&target), 0)?;
    Ok(target)
}

/// Whether a consumer saves where it is (Kafka) or acknowledges what it
/// read.
pub fn log(kind: &str) -> bool {
    kind == "kafka" || kind == "eventhub"
}

pub const KINDS: &[&str] = &["kafka", "eventhub", "rabbit", "websocket"];
