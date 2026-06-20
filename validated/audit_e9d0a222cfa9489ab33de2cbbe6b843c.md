### Title
Malformed Block Extension Field in `SendBlock` P2P Message Causes Node Panic — (`util/gen-types/src/extension/shortcut.rs`)

---

### Summary

The `BlockReader::extension()` method unconditionally calls `.unwrap()` on user-supplied block extension data. The synchronizer's `SendBlock` handler validates only that the block has at most one extra field, but never calls `check_data()` to verify the extension's internal molecule encoding. A malformed extension that passes the field-count check but is not a valid `Bytes` molecule encoding reaches `BlockExtensionVerifier::verify()`, which calls `block.extension()`, triggering a panic and crashing the node.

---

### Finding Description

`BlockReader::extension()` is documented to panic:

```rust
/// # Panics
///
/// Panics if the first extra field exists but not a valid [`BytesReader`].
pub fn extension(&self) -> Option<packed::BytesReader<'_>> {
    self.extra_field(0)
        .map(|data| packed::BytesReader::from_slice(data).unwrap())
}
``` [1](#0-0) 

The synchronizer's `SendBlock` handler guards only against `count_extra_fields() > 1`:

```rust
if reader.has_extra_fields() || reader.block().count_extra_fields() > 1 {
    // ban peer and return
} else {
    item  // passes through with exactly 1 extra field
}
``` [2](#0-1) 

A block with exactly one extra field (the extension slot) that is **not** a valid `Bytes` molecule encoding passes this guard. No `check_data()` call is made on the block in the synchronizer path, unlike the relayer path which explicitly calls `reader.check_data()` before processing `CompactBlock` and `BlockTransactions` messages. [3](#0-2) 

The block then proceeds to contextual verification, where `BlockExtensionVerifier::verify()` calls `block.extension()`:

```rust
let extension = if let Some(data) = block.extension() {
    data
} else {
    return Err(BlockErrorKind::UnknownFields.into());
};
``` [4](#0-3) 

If the extension field exists but is not a valid `Bytes` encoding, `from_slice(...).unwrap()` panics, crashing the node process.

The test suite confirms that `check_data()` is the intended guard for this case — `send_block_check_data_rejects_malformed_block_extension` verifies that `check_data()` rejects malformed extensions — but `check_data()` is never invoked in the synchronizer's `SendBlock` processing path. [5](#0-4) 

---

### Impact Explanation

An unprivileged P2P peer can send a crafted `SendBlock` message containing a block whose extension field is structurally valid at the molecule table level (passes `from_compatible_slice`) but whose content is not a valid `Bytes` encoding. This causes an unhandled panic in the block verification thread, crashing the CKB node. The attacker needs no keys, no stake, and no special role — only a P2P connection.

---

### Likelihood Explanation

The `SendBlock` message is a standard sync protocol message accepted from any connected peer. The malformed payload is trivially constructable: take any valid block, append one extra field whose bytes do not satisfy `BytesReader::from_slice`, and send it. The guard at the synchronizer entry point only checks the field count, not the field content. This is a low-effort, high-reliability crash trigger.

---

### Recommendation

- **Short term:** Add a `check_data()` call on the block inside the `SendBlock` branch of the synchronizer's `received()` handler, mirroring the pattern already used for `CompactBlock` and `BlockTransactions` in the relayer. Ban the peer on failure.
- **Long term:** Replace the `.unwrap()` in `BlockReader::extension()` with a graceful `?`/`ok()?` return so that even if `check_data()` is bypassed in a future code path, the panic cannot propagate. Add a fuzz test that sends `SendBlock` messages with malformed extension fields.

---

### Proof of Concept

1. Construct a valid `Block` molecule value.
2. Append one extra field whose bytes are `[0x02, 0x00, 0x00, 0x00, 0xFF]` — this satisfies the molecule total-size header (length = 5) but is not a valid `Bytes` encoding (the inner length prefix claims 2 bytes of content but only 1 byte follows).
3. Wrap it in a `SendBlock` → `SyncMessage` molecule envelope.
4. Send it to any CKB node over the sync P2P protocol.
5. The node passes the `count_extra_fields() > 1` guard (count == 1), proceeds to `BlockExtensionVerifier::verify()`, calls `block.extension()`, hits `.unwrap()` on the malformed slice, and panics. [6](#0-5)

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L246-254)
```rust
    /// Gets the extension field if it existed.
    ///
    /// # Panics
    ///
    /// Panics if the first extra field exists but not a valid [`BytesReader`](struct.BytesReader.html).
    pub fn extension(&self) -> Option<packed::BytesReader<'_>> {
        self.extra_field(0)
            .map(|data| packed::BytesReader::from_slice(data).unwrap())
    }
```

**File:** sync/src/synchronizer/mod.rs (L899-917)
```rust
                if let packed::SyncMessageUnionReader::SendBlock(ref reader) = item {
                    if reader.has_extra_fields() || reader.block().count_extra_fields() > 1 {
                        info!(
                            "A malformed message from peer {}: \
                             excessive fields detected in SendBlock",
                            peer_index
                        );
                        nc.ban_peer(
                            peer_index,
                            BAD_MESSAGE_BAN_TIME,
                            String::from(
                                "send us a malformed message: \
                                 too many fields in SendBlock",
                            ),
                        );
                        return;
                    } else {
                        item
                    }
```

**File:** sync/src/relayer/mod.rs (L126-134)
```rust
            packed::RelayMessageUnionReader::CompactBlock(reader) => {
                if reader.check_data() {
                    CompactBlockProcess::new(reader, self, nc, peer)
                        .execute()
                        .await
                } else {
                    StatusCode::ProtocolMessageIsMalformed.with_context("CompactBlock is invalid")
                }
            }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L550-555)
```rust
            1 => {
                let extension = if let Some(data) = block.extension() {
                    data
                } else {
                    return Err(BlockErrorKind::UnknownFields.into());
                };
```

**File:** util/gen-types/src/extension/tests/check_data.rs (L7-30)
```rust
#[cfg(test)]
fn append_malformed_bytes_extra_field(entity: &[u8], field_count: usize) -> Vec<u8> {
    let old_total_size = molecule::unpack_number(entity) as usize;
    let new_offset_size = molecule::NUMBER_SIZE;
    let malformed_bytes = [2, 0, 0, 0, 0];
    let new_total_size = old_total_size + new_offset_size + malformed_bytes.len();
    let extra_field_start = old_total_size + new_offset_size;

    let mut data = Vec::with_capacity(new_total_size);
    data.extend_from_slice(&molecule::pack_number(new_total_size as molecule::Number));

    for index in 0..field_count {
        let offset_index = molecule::NUMBER_SIZE * (index + 1);
        let offset = molecule::unpack_number(&entity[offset_index..]) as usize;
        data.extend_from_slice(&molecule::pack_number(
            (offset + new_offset_size) as molecule::Number,
        ));
    }
    data.extend_from_slice(&molecule::pack_number(
        extra_field_start as molecule::Number,
    ));
    data.extend_from_slice(&entity[molecule::NUMBER_SIZE * (field_count + 1)..]);
    data.extend_from_slice(&malformed_bytes);
    data
```

**File:** util/gen-types/src/extension/tests/check_data.rs (L63-76)
```rust
#[test]
fn send_block_check_data_rejects_malformed_block_extension() {
    let block = packed::Block::default();
    let block_with_malformed_extension =
        append_malformed_bytes_extra_field(block.as_slice(), packed::Block::FIELD_COUNT);
    let send_block = packed::SendBlock::new_builder()
        .block(packed::Block::new_unchecked(
            block_with_malformed_extension.into(),
        ))
        .build();

    let reader = packed::SendBlockReader::from_compatible_slice(send_block.as_slice()).unwrap();
    assert!(!reader.check_data());
}
```
