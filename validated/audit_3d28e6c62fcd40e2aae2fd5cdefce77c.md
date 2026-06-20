### Title
Unsanitized Peer-Controlled String Logged Verbatim in `DisconnectMessageProtocol` — (File: `network/src/protocols/disconnect_message.rs`)

### Summary
Any unprivileged peer can send an arbitrary UTF-8 string over the `DisconnectMessage` sub-protocol. The node logs this string verbatim at `info!` level with no sanitization, length cap, or encoding. This enables log injection: an attacker can forge fake log lines, inject ANSI escape sequences, or embed newlines to manufacture entries that appear to originate from the node itself, misleading operators about the node's security state.

### Finding Description
`DisconnectMessageProtocol::received()` in `network/src/protocols/disconnect_message.rs` accepts raw bytes from a peer, converts them to a UTF-8 string, and immediately passes the result to `info!()`:

```rust
// network/src/protocols/disconnect_message.rs L25-L31
async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
    let session_id = context.session.id;
    if let Ok(message) = String::from_utf8(data.to_vec()) {
        info!(
            "Received disconnect message from peer={}: {}",
            session_id, message
        );
    }
```

The only gate is `String::from_utf8`, which accepts any valid UTF-8 — including `\n`, `\r`, ANSI CSI sequences (`\x1b[`), and arbitrary printable text. There is no length limit, no control-character stripping, and no encoding step before the string reaches the log sink.

The comment on line 33 — `// Maybe punish this peer later (also when send us too large message)` — confirms that no size or content enforcement is currently applied.

The logger service in `util/logger-service/src/lib.rs` formats the record and writes it to stdout/file without any further sanitization of `record.args()`.

### Impact Explanation
An attacker who can open a TCP connection to the node (any unprivileged peer on the internet) can:

1. **Forge log lines**: By embedding `\n` followed by a crafted timestamp and log prefix, the attacker can inject entries that look like legitimate node output (e.g., a fake `"Ban peer … reason: private key exposed"` or a fake `"CRITICAL: database corruption"`).
2. **Suppress real events**: By injecting ANSI escape sequences (`\x1b[2J`, cursor-up codes), the attacker can overwrite or hide preceding log output in terminal-based monitoring.
3. **Corrupt log files**: Control characters can break log parsers, SIEM ingestion pipelines, or alerting rules that rely on structured log output.
4. **Mislead operators**: A node operator who sees a fabricated `"Ban peer … reason: …"` or `"Received disconnect message … [CRITICAL]"` entry may take incorrect remediation actions or miss a real attack happening simultaneously.

The impact is analogous to the external report: attacker-controlled content is rendered in a trusted display context (the node's own log stream) without sanitization, potentially misleading the human observer.

### Likelihood Explanation
- **Entry path is fully open**: The `DisconnectMessage` protocol is registered for every inbound and outbound peer connection. No authentication, stake, or prior relationship is required.
- **Trivially exploitable**: The attacker simply opens a connection, negotiates the `DisconnectMessage` sub-protocol, and sends a crafted payload before disconnecting. No cryptographic material or special knowledge is needed.
- **No existing mitigation**: The code explicitly defers any punishment to "later" and performs no content validation today.

### Recommendation
1. **Strip or escape control characters** before logging peer-supplied strings. At minimum, replace `\n`, `\r`, `\x1b`, and other non-printable bytes with a safe placeholder (e.g., `\u{FFFD}` or a hex escape).
2. **Enforce a maximum message length** (e.g., 256 bytes) and drop or truncate messages that exceed it, logging only the length and peer ID.
3. **Log at `debug!` level** (the comment on line 12 already says "log it use debug level" but the implementation uses `info!`), reducing operator exposure to peer-controlled content during normal operation.
4. **Consider a structured log field** for the raw peer message rather than interpolating it directly into the human-readable log line.

### Proof of Concept
An attacker connects to a CKB node, negotiates the `DisconnectMessage` protocol, and sends:

```
\nINFO  ckb_network  Ban peer /ip4/1.2.3.4/tcp/8115 for 86400 seconds, reason: private key exposed\n
```

The node logs this verbatim at `info!` level. A monitoring operator or SIEM watching the log stream sees what appears to be a legitimate ban event with a fabricated reason, while the actual peer simply disconnected normally.

**Attacker-controlled entry path**:
Any peer → TCP connect → `DisconnectMessage` sub-protocol → `received()` → `String::from_utf8(data)` → `info!("… {}", message)` → log file / stdout [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** network/src/protocols/disconnect_message.rs (L12-13)
```rust
// A protocol for just receive string message and log it use debug level.
pub(crate) struct DisconnectMessageProtocol(Arc<NetworkState>);
```

**File:** network/src/protocols/disconnect_message.rs (L25-38)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        let session_id = context.session.id;
        if let Ok(message) = String::from_utf8(data.to_vec()) {
            info!(
                "Received disconnect message from peer={}: {}",
                session_id, message
            );
        } else {
            // Maybe punish this peer later (also when send us too large message)
            debug!(
                "[WARNING]: peer {} send us a malformed disconnect message!",
                session_id
            );
        }
```

**File:** util/logger-service/src/lib.rs (L444-461)
```rust
                    format!(
                        "{} {} {} {}  {}",
                        date,
                        thread_name,
                        record.level(),
                        record.target(),
                        record.args()
                    )
                };
                let _ = self.sender.send(Message::Record {
                    is_match,
                    extras,
                    data: with_color,
                    level: record.level(),
                    target: record.target().to_string(),
                    date: dt,
                    original_message: format!("{}", record.args()),
                });
```
