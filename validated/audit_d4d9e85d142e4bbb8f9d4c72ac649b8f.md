The code is fully available. Let me analyze the exact state transitions.

**`handle_post_new_message` (legacy path):** [1](#0-0) 

It checks `emitter_type != Executable` but then: [2](#0-1) 

It increments the sequence and serializes — **without ever setting `emitter_type = Legacy`**. The type remains `Unset` after a successful legacy post.

**`handle_post_prepared_message` (executable path):** [3](#0-2) 

This path *does* set `emitter_type` (to `Legacy` or `Executable`). Crucially, when `emitter_type == Unset`, the check `!= Legacy` passes, and it sets `emitter_type = Executable`.

**The actual bug is not a race condition.** On Solana, transactions sharing a writable account are serialized within a slot — there is no true concurrency. The vulnerability is a **missing state transition**: `handle_post_new_message` never writes `EmitterType::Legacy` back to the account.

**Sequential exploit path (no race needed):**
1. Keypair emitter calls `post_message` (new message path) → message published at sequence 0, `emitter_type` written back as `Unset`.
2. Any program prepares a message with `info.emitter` set to that same keypair address and `info.emitter_authority` set to a different key (the program's PDA).
3. Program calls `post_message` (prepared message path) → check `!= Legacy` passes (type is `Unset`), sets `emitter_type = Executable`, publishes at sequence 1.
4. All future legacy posts from the keypair now fail with `ExecutableEmitter`.
5. The program can continue publishing messages under the keypair's emitter address.

**Guard that should exist but doesn't:** [2](#0-1) 

A single line `emitter_sequence.emitter_type = EmitterType::Legacy;` before serialization would close this. The `EmitterType` enum and the `Unset` variant exist precisely to handle the first-use case: [4](#0-3) 

---

### Title
Missing `EmitterType::Legacy` assignment in `handle_post_new_message` allows emitter type corruption — (`target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/mod.rs`)

### Summary
`handle_post_new_message` checks that an emitter is not already `Executable` but never writes `EmitterType::Legacy` back to the `EmitterSequence` PDA. After a successful legacy post, the type remains `Unset`, allowing a subsequent `handle_post_prepared_message` call to claim the same emitter PDA as `Executable`.

### Finding Description
In `handle_post_new_message` (lines 134–219 of `post_message/mod.rs`), after verifying `emitter_type != Executable` and incrementing the sequence, the function serializes the `EmitterSequence` struct without setting `emitter_type = Legacy`. The `Unset` value is written back to chain. In `handle_post_prepared_message` (lines 225–332), the guard at line 265 only rejects `emitter_type == Legacy`; an `Unset` type passes, and the function then writes `emitter_type = Executable`. The invariant — that an emitter's type is permanently fixed after first use — is violated sequentially, with no concurrency required.

### Impact Explanation
- A program can publish messages under a keypair emitter's address, impersonating it to Wormhole Guardians.
- After the executable post, all future legacy posts from the keypair fail with `ExecutableEmitter`, causing a permanent denial-of-service against the original emitter.
- Downstream consumers that trust emitter-type binding (e.g., to distinguish program-controlled from keypair-controlled emitters) receive corrupted data.

### Likelihood Explanation
The attack requires only: (1) observing a keypair emitter's first-ever post (publicly visible on-chain), and (2) submitting a prepared message with the same emitter address before any second legacy post locks the type. No privileged access, leaked keys, or governance majority is needed. The window is open indefinitely after step 1 since the type stays `Unset` forever until a prepared-message post occurs.

### Recommendation
Add `emitter_sequence.emitter_type = EmitterType::Legacy;` in `handle_post_new_message` immediately before the serialization block (after line 203), mirroring the assignment already present in `handle_post_prepared_message` at line 263.

### Proof of Concept
```rust
// 1. Keypair emitter posts a new message (legacy path).
//    After this, emitter_sequence.emitter_type == Unset (bug).
post_message(ctx_legacy, args_legacy)?;

// 2. Program prepares a message with emitter = keypair_pubkey,
//    emitter_authority = program_pda (different key → executable branch).
init_message_v1(...)?;
write_message_v1(...)?;
finalize_message_v1(...)?;  // status = ReadyForPublishing

// 3. Program calls post_message with the prepared message.
//    handle_post_prepared_message: emitter != emitter_authority,
//    check (emitter_type != Legacy) passes (Unset), sets emitter_type = Executable.
post_message(ctx_executable, args_empty)?;

// 4. Assert: emitter_sequence.emitter_type == Executable
// 5. Keypair emitter attempts another post → fails with ExecutableEmitter.
// 6. Program continues posting under keypair's emitter address indefinitely.
```

### Citations

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/mod.rs (L151-154)
```rust
    require!(
        emitter_sequence.emitter_type != EmitterType::Executable,
        CoreBridgeError::ExecutableEmitter
    );
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/mod.rs (L202-209)
```rust
    // Update emitter sequence account with incremented value.
    {
        emitter_sequence.value += 1;

        let acc_data: &mut [_] = &mut ctx.accounts.emitter_sequence.data.borrow_mut();
        let mut writer = std::io::Cursor::new(acc_data);
        emitter_sequence.try_serialize(&mut writer)?;
    }
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/mod.rs (L258-270)
```rust
    if info.emitter == info.emitter_authority {
        require!(
            emitter_sequence.emitter_type != EmitterType::Executable,
            CoreBridgeError::ExecutableEmitter
        );
        emitter_sequence.emitter_type = EmitterType::Legacy;
    } else {
        require!(
            emitter_sequence.emitter_type != EmitterType::Legacy,
            CoreBridgeError::LegacyEmitter
        );
        emitter_sequence.emitter_type = EmitterType::Executable;
    }
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/state/emitter_sequence.rs (L31-36)
```rust
#[derive(Debug, AnchorSerialize, AnchorDeserialize, Clone, PartialEq, Eq, InitSpace)]
pub enum EmitterType {
    Unset,
    Legacy,
    Executable,
}
```
