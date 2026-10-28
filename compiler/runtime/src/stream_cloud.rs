//! nexo.stream on the clouds' own services (section 19, D168), as phase
//! 0's stream_cloud.py: `sqs` and `kinesis` on AWS (signed with SigV4),
//! `servicebus` on Azure (with a token of Azure AD) and `pubsub` on Google
//! Cloud, each through its HTTP API, with the credentials and addresses
//! nexo.infra reads. A queue is read with a lease and each message deleted
//! once it is handled; a Kinesis stream is read from the sequence number
//! the consumer group saved in .nexo/streams, like the local broker.

use std::sync::Mutex;

use sha2::{Digest, Sha256};

use crate::infra_cloud::{error_text, need_env, sigv4};
use crate::json::{self, Json};
use crate::state::{failure_as, R};
use crate::stream::{kept, Id, Message};

const TIME: &str = "nexo-time";
const KEY: &str = "nexo-key";

type Pair = (String, String);

struct Answer {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Answer {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
    fn json(&self) -> Json {
        let text = String::from_utf8_lossy(&self.body).to_string();
        json::parse(if text.trim().is_empty() { "{}" } else { &text }).unwrap_or(Json::Obj(Vec::new()))
    }
}

/// (status, headers, body); a network failure is NetworkError.
fn request(method: &str, url: &str, headers: &[(String, String)], body: Option<&[u8]>, span: u32) -> R<Answer> {
    let mut b = ureq::AgentBuilder::new().redirects(0).timeout(std::time::Duration::from_secs(60)).user_agent("nexo/0.1");
    if let Some(p) = crate::http::proxy_for(url) {
        b = b.proxy(p);
    }
    let mut req = b.build().request(method, url);
    for (k, v) in headers {
        req = req.set(k, v);
    }
    let result = match body {
        Some(data) => req.send_bytes(data),
        None => req.call(),
    };
    let resp = match result {
        Ok(r) => r,
        Err(ureq::Error::Status(_, r)) => r,
        Err(ureq::Error::Transport(t)) => return Err(crate::http::network(url, crate::http::transport_reason(&t), span)),
    };
    let status = resp.status();
    let mut hs = Vec::new();
    for name in resp.headers_names() {
        if let Some(v) = resp.header(&name) {
            hs.push((name.to_lowercase(), v.to_string()));
        }
    }
    let mut data = Vec::new();
    use std::io::Read;
    let _ = resp.into_reader().read_to_end(&mut data);
    Ok(Answer { status, headers: hs, body: data })
}

fn refused(s: &Pair, a: &Answer, span: u32) -> crate::state::Fail {
    let shown = format!("{}(\"{}\")", s.0, s.1);
    failure_as(
        "HTTPError",
        "HTTPError.stream",
        span,
        &[("stream", shown), ("status", a.status.to_string()), ("reason", error_text(&a.body))],
    )
}

fn checked(s: &Pair, a: Answer, span: u32) -> R<Answer> {
    if a.status >= 400 {
        return Err(refused(s, &a, span));
    }
    Ok(a)
}

fn text(j: &Json) -> Option<String> {
    j.str().map(|s| s.to_string())
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// The host a source talks to (for the policy's `net`).
pub fn host(kind: &str, place: &str) -> String {
    let url = match kind {
        "sqs" | "kinesis" => aws_endpoint(kind, place),
        "servicebus" => azure_url(&format!("https://{place}.servicebus.windows.net/")),
        _ => pubsub_base(),
    };
    crate::http::url_host(&url)
}

// ---------------------------------------------------------------- AWS

fn aws_endpoint(service: &str, region: &str) -> String {
    match std::env::var("AWS_ENDPOINT_URL").ok().filter(|v| !v.is_empty()) {
        Some(e) => e.trim_end_matches('/').to_string(),
        None => format!("https://{service}.{region}.amazonaws.com"),
    }
}

fn aws(s: &Pair, region: &str, op: &str, payload: Json, span: u32) -> R<Json> {
    let service = s.0.as_str();
    let url = format!("{}/", aws_endpoint(service, region));
    let body = payload.compact().into_bytes();
    let (ctype, prefix) = if service == "sqs" {
        ("application/x-amz-json-1.0", "AmazonSQS")
    } else {
        ("application/x-amz-json-1.1", "Kinesis_20131202")
    };
    let headers = [("Content-Type", ctype.to_string()), ("X-Amz-Target", format!("{prefix}.{op}"))];
    let signed = sigv4("POST", &url, &headers, &body, service, region)?;
    let a = checked(s, request("POST", &url, &signed, Some(&body), span)?, span)?;
    Ok(a.json())
}

static QUEUE_URLS: Mutex<Vec<(String, String)>> = Mutex::new(Vec::new());
static SHARDS: Mutex<Vec<(String, Vec<String>)>> = Mutex::new(Vec::new());

fn sqs_url(s: &Pair, region: &str, name: &str, span: u32) -> R<String> {
    let k = format!("{region}/{name}");
    if let Some((_, u)) = QUEUE_URLS.lock().unwrap().iter().find(|(x, _)| *x == k) {
        return Ok(u.clone());
    }
    let out = aws(s, region, "GetQueueUrl", json::obj(vec![("QueueName", json::s(name.to_string()))]), span)?;
    let url = out.get("QueueUrl").str().unwrap_or("").to_string();
    QUEUE_URLS.lock().unwrap().push((k, url.clone()));
    Ok(url)
}

fn shards(s: &Pair, region: &str, span: u32) -> R<Vec<String>> {
    let k = format!("{region}/{}", s.1);
    if let Some((_, v)) = SHARDS.lock().unwrap().iter().find(|(x, _)| *x == k) {
        return Ok(v.clone());
    }
    let out = aws(s, region, "ListShards", json::obj(vec![("StreamName", json::s(s.1.clone()))]), span)?;
    let ids: Vec<String> = match out.get("Shards") {
        Json::Arr(items) => items.iter().filter_map(|x| text(x.get("ShardId"))).collect(),
        _ => Vec::new(),
    };
    SHARDS.lock().unwrap().push((k, ids.clone()));
    Ok(ids)
}

fn kinesis_path(s: &Pair, partition: i64) -> String {
    format!(".nexo/streams/kinesis/{}.{partition}.{}.offset", s.1, crate::stream::group_name())
}

// -------------------------------------------------------------- Azure

/// A data-plane address, at NEXO_AZURE_ENDPOINT when it is set (an
/// emulator): https://HOST/path becomes ENDPOINT/HOST/path.
fn azure_url(url: &str) -> String {
    match std::env::var("NEXO_AZURE_ENDPOINT").ok().filter(|v| !v.is_empty()) {
        Some(e) if url.starts_with("https://") => format!("{}/{}", e.trim_end_matches('/'), &url[8..]),
        _ => url.to_string(),
    }
}

fn sb_base(namespace: &str, name: &str) -> String {
    azure_url(&format!("https://{namespace}.servicebus.windows.net/{name}"))
}

const SB_SCOPE: &str = "https://servicebus.azure.net/.default";

/// The tokens of Azure AD, by scope.
static AZURE_TOKENS: Mutex<Vec<(String, String)>> = Mutex::new(Vec::new());

/// A token of Azure AD for a service (client credentials).
pub fn azure_token(s: &Pair, scope: &str, span: u32) -> R<String> {
    if let Some((_, t)) = AZURE_TOKENS.lock().unwrap().iter().find(|(k, _)| k == scope) {
        return Ok(t.clone());
    }
    let tenant = need_env("AZURE_TENANT_ID")?;
    let client = need_env("AZURE_CLIENT_ID")?;
    let secret = need_env("AZURE_CLIENT_SECRET")?;
    let host = std::env::var("AZURE_AUTHORITY_HOST").ok().filter(|v| !v.is_empty()).unwrap_or("https://login.microsoftonline.com".into());
    let url = format!("{}/{tenant}/oauth2/v2.0/token", host.trim_end_matches('/'));
    let enc = |v: &str| -> String {
        v.bytes()
            .map(|b| if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) { (b as char).to_string() } else { format!("%{b:02X}") })
            .collect()
    };
    let form = format!(
        "grant_type=client_credentials&client_id={}&client_secret={}&scope={}",
        enc(&client),
        enc(&secret),
        enc(scope)
    );
    let headers = [("Content-Type".to_string(), "application/x-www-form-urlencoded".to_string())];
    let a = checked(s, request("POST", &url, &headers, Some(form.as_bytes()), span)?, span)?;
    let token = a.json().get("access_token").str().unwrap_or("").to_string();
    AZURE_TOKENS.lock().unwrap().push((scope.to_string(), token.clone()));
    Ok(token)
}

// --------------------------------------------------------- Google Cloud

fn pubsub_base() -> String {
    if let Some(h) = std::env::var("PUBSUB_EMULATOR_HOST").ok().filter(|v| !v.is_empty()) {
        return format!("http://{h}/v1");
    }
    match std::env::var("NEXO_GCP_ENDPOINT").ok().filter(|v| !v.is_empty()) {
        Some(e) => format!("{}/pubsub/v1", e.trim_end_matches('/')),
        None => "https://pubsub.googleapis.com/v1".into(),
    }
}

fn pubsub(method: &str, path: &str, payload: Option<Json>, span: u32) -> R<Answer> {
    let mut headers = vec![("Content-Type".to_string(), "application/json".to_string())];
    if std::env::var("PUBSUB_EMULATOR_HOST").ok().filter(|v| !v.is_empty()).is_none() {
        headers.push(("Authorization".into(), format!("Bearer {}", need_env("GOOGLE_OAUTH_ACCESS_TOKEN")?)));
    }
    let body = payload.map(|p| p.compact().into_bytes());
    request(method, &format!("{}/{path}", pubsub_base()), &headers, body.as_deref(), span)
}

fn subscription(s: &Pair, project: &str) -> String {
    format!("projects/{project}/subscriptions/{}.{}", s.1, crate::stream::group_name())
}

// ----------------------------------------------------------- the calls

/// How many partitions a stream of a cloud has: a Kinesis stream's
/// shards, 1 for a queue (and a Pub/Sub topic, whose subscription of the
/// consumer group is made when it is missing).
pub fn partitions(s: &Pair, place: &str, span: u32) -> R<i64> {
    match s.0.as_str() {
        "sqs" => {
            sqs_url(s, place, &s.1, span)?;
            Ok(1)
        }
        "kinesis" => Ok(shards(s, place, span)?.len() as i64),
        "pubsub" => {
            let topic = format!("projects/{place}/topics/{}", s.1);
            let body = json::obj(vec![("topic", json::s(topic)), ("ackDeadlineSeconds", Json::Int(60))]);
            let a = pubsub("PUT", &subscription(s, place), Some(body), span)?;
            if a.status >= 400 && a.status != 409 {
                return Err(refused(s, &a, span));
            }
            Ok(1)
        }
        _ => Ok(1),
    }
}

/// Where a consumer group starts: the sequence number it saved (Kinesis).
pub fn start(s: &Pair, partition: i64) -> Option<String> {
    if s.0 != "kinesis" {
        return None;
    }
    std::fs::read_to_string(kinesis_path(s, partition)).ok().map(|t| t.trim().to_string()).filter(|t| !t.is_empty())
}

fn sent(s: &Pair, place: &str, id: Id, ms: i64, data: &str, key: Option<&str>) -> Message {
    Message {
        security: crate::security::context::capture(),
        stream: s.clone(),
        place: Some(place.to_string()),
        id,
        ms,
        data: data.to_string(),
        key: key.map(|k| k.to_string()),
        partition: 0,
        before: None,
        receipt: None,
    }
}

/// `send` to a stream of a cloud (or to `name`, its dead letters).
pub fn send(s: &Pair, place: &str, data: &str, ms: i64, key: Option<&str>, name: Option<&str>, span: u32) -> R<Message> {
    let target = name.unwrap_or(&s.1).to_string();
    match s.0.as_str() {
        "sqs" => {
            let mut attrs = vec![(TIME, json::obj(vec![("DataType", json::s("Number")), ("StringValue", json::s(ms.to_string()))]))];
            if let Some(k) = key {
                attrs.push((KEY, json::obj(vec![("DataType", json::s("String")), ("StringValue", json::s(k.to_string()))])));
            }
            let mut payload = vec![
                ("QueueUrl", json::s(sqs_url(s, place, &target, span)?)),
                ("MessageBody", json::s(data.to_string())),
                ("MessageAttributes", json::obj(attrs)),
            ];
            if target.ends_with(".fifo") {
                // a FIFO queue keeps the order of each group: the key
                payload.push(("MessageGroupId", json::s(key.unwrap_or("nexo").to_string())));
                payload.push(("MessageDeduplicationId", json::s(hex(&Sha256::digest(format!("{ms}:{data}").as_bytes())))));
            }
            let out = aws(s, place, "SendMessage", json::obj(payload), span)?;
            Ok(sent(s, place, text(out.get("MessageId")).map(Id::Text).unwrap_or(Id::None), ms, data, key))
        }
        "kinesis" => {
            // the shard is chosen by the partition key: the key, or the data
            let part = match key {
                Some(k) => k.to_string(),
                None => hex(&Sha256::digest(data.as_bytes()))[..32].to_string(),
            };
            let payload = json::obj(vec![
                ("StreamName", json::s(target)),
                ("PartitionKey", json::s(part)),
                ("Data", json::s(crate::crypto::b64(data.as_bytes()))),
            ]);
            let out = aws(s, place, "PutRecord", payload, span)?;
            Ok(sent(s, place, text(out.get("SequenceNumber")).map(Id::Text).unwrap_or(Id::None), ms, data, key))
        }
        "servicebus" => {
            let mut headers = vec![
                ("Authorization".to_string(), format!("Bearer {}", azure_token(s, SB_SCOPE, span)?)),
                ("Content-Type".to_string(), "text/plain; charset=utf-8".to_string()),
                (TIME.to_string(), ms.to_string()),
            ];
            if let Some(k) = key {
                headers.push((KEY.to_string(), json::s(k.to_string()).compact()));
            }
            let url = format!("{}/messages", sb_base(place, &target));
            checked(s, request("POST", &url, &headers, Some(data.as_bytes()), span)?, span)?;
            Ok(sent(s, place, Id::None, ms, data, key))
        }
        _ => {
            let mut attrs = vec![(TIME, json::s(ms.to_string()))];
            if let Some(k) = key {
                attrs.push((KEY, json::s(k.to_string())));
            }
            let m = json::obj(vec![("data", json::s(crate::crypto::b64(data.as_bytes()))), ("attributes", json::obj(attrs))]);
            let topic = format!("projects/{place}/topics/{target}");
            let a = checked(s, pubsub("POST", &format!("{topic}:publish"), Some(json::obj(vec![("messages", Json::Arr(vec![m]))])), span)?, span)?;
            let id = match a.json().get("messageIds") {
                Json::Arr(ids) => ids.first().and_then(text).map(Id::Text).unwrap_or(Id::None),
                _ => Id::None,
            };
            Ok(sent(s, place, id, ms, data, key))
        }
    }
}

fn unb64(t: &str) -> String {
    String::from_utf8_lossy(&crate::crypto::unb64(t).unwrap_or_default()).into_owned()
}

/// The messages a consumer gets now from a partition.
pub fn poll(s: &Pair, place: &str, partition: i64, position: &Option<String>, span: u32) -> R<Vec<Message>> {
    let mut out = Vec::new();
    let message = |id: Id, ms: i64, data: String, key: Option<String>, before: Option<String>, receipt: Option<String>| Message {
        security: crate::security::context::capture(),
        stream: s.clone(),
        place: Some(place.to_string()),
        id,
        ms,
        data,
        key,
        partition,
        before,
        receipt,
    };
    match s.0.as_str() {
        "sqs" => {
            let url = sqs_url(s, place, &s.1, span)?;
            let payload = json::obj(vec![
                ("QueueUrl", json::s(url.clone())),
                ("MaxNumberOfMessages", Json::Int(10)),
                ("WaitTimeSeconds", Json::Int(1)),
                ("VisibilityTimeout", Json::Int(60)),
                ("MessageAttributeNames", Json::Arr(vec![json::s("All")])),
                ("AttributeNames", Json::Arr(vec![json::s("SentTimestamp")])),
            ]);
            let got = aws(s, place, "ReceiveMessage", payload, span)?;
            if let Json::Arr(items) = got.get("Messages") {
                for m in items {
                    let attrs = m.get("MessageAttributes");
                    let ms = text(attrs.get(TIME).get("StringValue"))
                        .or_else(|| text(m.get("Attributes").get("SentTimestamp")))
                        .and_then(|t| t.parse().ok())
                        .unwrap_or(0);
                    let key = text(attrs.get(KEY).get("StringValue"));
                    let receipt = format!("{url}\n{}", text(m.get("ReceiptHandle")).unwrap_or_default());
                    let id = text(m.get("MessageId")).map(Id::Text).unwrap_or(Id::None);
                    out.push(message(id, ms, text(m.get("Body")).unwrap_or_default(), key, None, Some(receipt)));
                }
            }
        }
        "kinesis" => {
            let shard = shards(s, place, span)?.get(partition as usize).cloned().unwrap_or_default();
            let mut start = vec![("StreamName", json::s(s.1.clone())), ("ShardId", json::s(shard))];
            match position {
                None => start.push(("ShardIteratorType", json::s("TRIM_HORIZON"))),
                Some(p) => {
                    start.push(("ShardIteratorType", json::s("AFTER_SEQUENCE_NUMBER")));
                    start.push(("StartingSequenceNumber", json::s(p.clone())));
                }
            }
            let it = aws(s, place, "GetShardIterator", json::obj(start), span)?.get("ShardIterator").str().unwrap_or("").to_string();
            let got = aws(s, place, "GetRecords", json::obj(vec![("ShardIterator", json::s(it)), ("Limit", Json::Int(100))]), span)?;
            let mut before = position.clone();
            if let Json::Arr(records) = got.get("Records") {
                for r in records {
                    let seq = text(r.get("SequenceNumber")).unwrap_or_default();
                    let secs = match r.get("ApproximateArrivalTimestamp") {
                        Json::Num(f) => *f,
                        Json::Int(n) => *n as f64,
                        _ => 0.0,
                    };
                    let data = unb64(r.get("Data").str().unwrap_or(""));
                    out.push(message(Id::Text(seq.clone()), (secs * 1000.0) as i64, data, text(r.get("PartitionKey")), before.clone(), None));
                    before = Some(seq);
                }
            }
        }
        "servicebus" => {
            let token = azure_token(s, SB_SCOPE, span)?;
            for _ in 0..10 {
                let headers = [("Authorization".to_string(), format!("Bearer {token}"))];
                let a = request("POST", &format!("{}/messages/head?timeout=1", sb_base(place, &s.1)), &headers, Some(b""), span)?;
                if a.status == 204 {
                    break;
                }
                let a = checked(s, a, span)?;
                let props = json::parse(a.header("brokerproperties").unwrap_or("{}")).unwrap_or(Json::Obj(Vec::new()));
                let ms = a.header(TIME).and_then(|t| t.parse().ok()).unwrap_or(0);
                let key = a.header(KEY).and_then(json::parse).and_then(|k| text(&k));
                let receipt = format!(
                    "{}/messages/{}/{}",
                    sb_base(place, &s.1),
                    text(props.get("MessageId")).unwrap_or_default(),
                    text(props.get("LockToken")).unwrap_or_default()
                );
                let id = props.get("SequenceNumber").int().map(Id::Int).unwrap_or(Id::None);
                out.push(message(id, ms, String::from_utf8_lossy(&a.body).into_owned(), key, None, Some(receipt)));
            }
        }
        _ => {
            let a = checked(s, pubsub("POST", &format!("{}:pull", subscription(s, place)), Some(json::obj(vec![("maxMessages", Json::Int(100))])), span)?, span)?;
            if let Json::Arr(items) = a.json().get("receivedMessages") {
                for r in items {
                    let m = r.get("message");
                    let attrs = m.get("attributes");
                    let ms = text(attrs.get(TIME)).and_then(|t| t.parse().ok()).unwrap_or(0);
                    let id = text(m.get("messageId")).map(Id::Text).unwrap_or(Id::None);
                    out.push(message(id, ms, unb64(m.get("data").str().unwrap_or("")), text(attrs.get(KEY)), None, text(r.get("ackId"))));
                }
            }
        }
    }
    Ok(out)
}

/// What is handled: deleted from a queue, or the position saved (Kinesis).
pub fn done(s: &Pair, place: &str, partition: i64, messages: &[Message], position: &Option<String>, span: u32) -> R<()> {
    match s.0.as_str() {
        "sqs" => {
            for m in messages {
                let (url, receipt) = m.receipt.as_deref().unwrap_or("").split_once('\n').unwrap_or(("", ""));
                aws(s, place, "DeleteMessage", json::obj(vec![("QueueUrl", json::s(url)), ("ReceiptHandle", json::s(receipt))]), span)?;
            }
        }
        "kinesis" => {
            if let Some(p) = kept(s, &Some(place.to_string()), partition, position.clone()) {
                let _ = std::fs::create_dir_all(".nexo/streams/kinesis");
                let _ = std::fs::write(kinesis_path(s, partition), format!("{p}\n"));
            }
        }
        "servicebus" => {
            for m in messages {
                let headers = [("Authorization".to_string(), format!("Bearer {}", azure_token(s, SB_SCOPE, span)?))];
                checked(s, request("DELETE", m.receipt.as_deref().unwrap_or(""), &headers, None, span)?, span)?;
            }
        }
        _ => {
            if !messages.is_empty() {
                let ids: Vec<Json> = messages.iter().filter_map(|m| m.receipt.clone()).map(json::s).collect();
                let path = format!("{}:acknowledge", subscription(s, place));
                checked(s, pubsub("POST", &path, Some(json::obj(vec![("ackIds", Json::Arr(ids))])), span)?, span)?;
            }
        }
    }
    Ok(())
}

/// Send a message that failed to its dead letters; the name they go to.
pub fn dead(m: &Message, place: &str) -> R<String> {
    let name = &m.stream.1;
    // SQS names cannot have a dot: the dead letters go to <name>-dead
    let target = match m.stream.0.as_str() {
        "sqs" if name.ends_with(".fifo") => format!("{}-dead.fifo", &name[..name.len() - 5]),
        "sqs" => format!("{name}-dead"),
        _ => format!("{name}.dead"),
    };
    send(&m.stream, place, &m.data, m.ms, m.key.as_deref(), Some(&target), 0)?;
    Ok(target)
}
