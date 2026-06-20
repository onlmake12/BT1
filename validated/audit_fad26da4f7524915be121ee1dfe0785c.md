### Title
Unsanitized Peer-Controlled Disconnect Message Injected Into Node Logs via `DisconnectMessageProtocol` - (File: `network/src/protocols/disconnect_message.rs`)

---

### Summary

Any unprivileged remote P2P peer can send an arbitrary UTF-8 string as a disconnect message. The CKB node logs this string verbatim at `info` level with no sanitization of newlines, carriage returns, or ANSI escape sequences. This allows a malicious peer to inject forged log entries or terminal control sequences into the node operator's log stream, misrepresenting node state.

---

### Finding Description

`DisconnectMessageProtocol::received()` accepts raw bytes from a remote peer, validates only that they form valid UTF-8, and immediately passes the result to `info!()`: [1](#0-0) 

```rust
if let Ok(message) = String::from_utf8(data.to_vec()) {
    info!(
        "Received disconnect message from peer={}: {}",
        session_id, message
    );
}
```

The only gate is `String::from_utf8()`, which accepts any valid Unicode — including `\n`, `\r`, `\x08` (backspace), and ANSI escape sequences such as `\x1b[2J` (clear screen) or `\x1b[31m` (red text). No further sanitization is applied before the string reaches the logger.

The logger in `util/logger-service/src/lib.rs` embeds `record.args()` directly into the formatted output string: [2](#0-1) 

```rust
format!(
    "{} {} {} {}  {}",
    date,
    thread_name,
    record.level(),
    record.target(),
    record.args()   // ← peer-controlled string lands here
)
```

When writing to stdout with color enabled, the raw `data` string (including any embedded escape codes) is printed directly: [3](#0-2) 

When writing to file, `sanitize_color()` is called, which strips ANSI SGR color codes but does **not** strip `\n`, `\r`, or other control characters: [4](#0-3) 

The logger uses `yansi::Paint` for ANSI terminal output: [5](#0-4) 

---

### Impact Explanation

A malicious peer crafts a disconnect message such as:

```
\n2024-01-01 00:00:00.000 +00:00 main INFO  ckb_chain  Block #99999999 accepted, chain reorganized
```

This injects a completely fabricated log line into the node's log stream at `info` level, indistinguishable from a genuine entry. Operators relying on log monitoring (alerting pipelines, SIEM, manual review) can be misled about chain state, peer bans, or security events.

With ANSI escape codes (stdout/terminal mode), a peer can:
- Clear the terminal screen (`\x1b[2J\x1b[H`)
- Overwrite previous log lines with `\r`
- Change text color to hide or highlight fabricated entries

The `original_message` field (used for log subscription notifications) also carries the unsanitized string: [6](#0-5) 

---

### Likelihood Explanation

- **No privilege required**: Any peer that can establish a TCP connection to the node's P2P port can send a disconnect message. The `DisconnectMessageProtocol` is registered for all peers.
- **Trivially reachable**: The attacker simply connects, sends a crafted `DisconnectMessage` protocol frame containing the payload string, and disconnects. No authentication, no prior state.
- **No length limit enforced in the handler**: The handler does not bound the message size before logging. [7](#0-6) 

---

### Recommendation

Sanitize the peer-supplied disconnect message before logging. At minimum, replace or escape `\n`, `\r`, `\x08`, and ANSI escape sequences (`\x1b[...`). A simple approach:

```rust
let sanitized = message
    .replace('\n', "\\n")
    .replace('\r', "\\r")
    .replace('\x1b', "\\x1b");
info!(
    "Received disconnect message from peer={}: {}",
    session_id, sanitized
);
```

Additionally, enforce a maximum length on the disconnect message (e.g., 256 bytes) before attempting UTF-8 conversion, and ban peers that exceed it, consistent with the existing comment at line 33.

---

### Proof of Concept

1. Connect to a CKB node's P2P port.
2. Perform the Tentacle handshake and open the `DisconnectMessage` protocol (protocol ID registered in `SupportProtocols::DisconnectMessage`). [8](#0-7) 

3. Send the following UTF-8 payload as the protocol frame body:

```
\n2024-01-01 00:00:00.000 +00:00 tokio-runtime-worker INFO  ckb_chain  CRITICAL: private key exposed, node shutting down
```

4. Observe the CKB node's log output. The injected line appears as a genuine `INFO ckb_chain` entry, with correct timestamp formatting, indistinguishable from real node output.

5. For terminal injection, send `\x1b[2J\x1b[H` to clear the operator's terminal screen, followed by fabricated status lines.

### Citations

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

**File:** util/logger-service/src/lib.rs (L21-21)
```rust
use yansi::Paint;
```

**File:** util/logger-service/src/lib.rs (L210-217)
```rust
                            let removed_color = if (is_match
                                && (!main_logger.color || main_logger.to_file))
                                || !extras.is_empty()
                            {
                                sanitize_color(data.as_ref())
                            } else {
                                "".to_owned()
                            };
```

**File:** util/logger-service/src/lib.rs (L227-234)
```rust
                                if main_logger.to_stdout {
                                    let output = if main_logger.color {
                                        data.as_str()
                                    } else {
                                        removed_color.as_str()
                                    };
                                    println!("{output}");
                                }
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

**File:** util/logger-service/src/lib.rs (L460-460)
```rust
                    original_message: format!("{}", record.args()),
```

**File:** network/src/network.rs (L1607-1621)
```rust
pub(crate) fn disconnect_with_message(
    control: &ServiceControl,
    peer_index: SessionId,
    message: &str,
) -> Result<(), SendErrorKind> {
    if !message.is_empty() {
        let data = Bytes::from(message.as_bytes().to_vec());
        // Must quick send, otherwise this message will be dropped.
        control.quick_send_message_to(
            peer_index,
            SupportProtocols::DisconnectMessage.protocol_id(),
            data,
        )?;
    }
    control.disconnect(peer_index)
```
