Looking at the code carefully to trace the exact attack path and verify each guard.

The code is fully readable. Here is the analysis:

---

### Title
Unprivileged Attacker Can Permanently Freeze Victim's SOL in `draft_message` Account via Front-Running `init_message_v1` — (`target_chains/solana/programs/core-bridge/src/processor/post_message/init_message_v1.rs`)

### Summary

`init_message_v1` imposes no constraint linking `emitter_authority` to the creator or funder of `draft_message`. Any signer can call `init_message_v1` on any zeroed account owned by the Core Bridge program. If an attacker front-runs the victim's call, the attacker's pubkey is permanently written as the stored `emitter_authority`, and neither the victim nor any third party can ever close or re-initialize the account.

### Finding Description

The `InitMessageV1` account struct requires only two things of `draft_message`: [1](#0-0) 

- `mut`
- `owner = crate::ID`

There is no PDA derivation from `emitter_authority`, no `has_one`, and no check that the signer funded the account. The `constraints` function only validates size and that the header bytes are all zeros: [2](#0-1) 

On success, the attacker's pubkey is written as `emitter_authority` with `status = MessageStatus::Writing`: [3](#0-2) 

After this, `close_message_v1` enforces `require_draft_message`, which checks that the signer's key matches the stored `emitter_authority`: [4](#0-3) 

The victim's key does not match the attacker's stored key, so `close_message_v1` always reverts with `EmitterAuthorityMismatch`. Re-calling `init_message_v1` also fails because the header is no longer zeroed (`AccountNotZeroed`). There is no admin recovery path.

### Impact Explanation

The victim's SOL (rent-exempt lamports for the `draft_message` account) is permanently locked. The Core Bridge program is the account owner; the system program cannot reclaim it. Only `close_message_v1` can drain the lamports, and it is permanently gated behind the attacker's signature. [5](#0-4) 

### Likelihood Explanation

The attack requires a front-running window between account creation (via system program) and the `init_message_v1` call. The SDK documentation explicitly states the account must be created before calling `prepare_message`: [6](#0-5) 

Integrators who create the account in a separate transaction (e.g., for large messages that exceed transaction size limits) are directly vulnerable. The attacker needs only their own keypair — no privileged access.

### Recommendation

Bind `draft_message` to `emitter_authority` at account creation time. The standard Solana pattern is to require `draft_message` to be a PDA derived from `emitter_authority` (and a nonce/seed), so only the holder of `emitter_authority` can ever produce a valid `draft_message` address. Alternatively, require the `emitter_authority` to be a co-signer on the system program `create_account` call in the same atomic transaction, eliminating the front-running window entirely.

### Proof of Concept

```
1. Victim calls SystemProgram::create_account(
       from: victim,
       to: draft_message_keypair,
       lamports: rent_exempt_amount,
       space: required_size,
       owner: core_bridge::ID
   )  ← Transaction A, broadcast to mempool

2. Attacker observes Transaction A, front-runs with:
   init_message_v1(
       emitter_authority: attacker_keypair,  ← attacker signs
       draft_message: draft_message_keypair.pubkey()
   )  ← succeeds: account is owned by Core Bridge, header is all zeros

3. Victim's init_message_v1 call → AccountNotZeroed (header no longer zeros)

4. Victim's close_message_v1 call → EmitterAuthorityMismatch
   (stored emitter_authority = attacker pubkey ≠ victim pubkey)

5. Victim's SOL is permanently locked. Attacker holds the only key
   that can close the account and has no incentive to do so.
```

### Citations

**File:** target_chains/solana/programs/core-bridge/src/processor/post_message/init_message_v1.rs (L17-21)
```rust
    #[account(
        mut,
        owner = crate::ID
    )]
    draft_message: UncheckedAccount<'info>,
```

**File:** target_chains/solana/programs/core-bridge/src/processor/post_message/init_message_v1.rs (L43-47)
```rust
        let msg_acc_data: &[_] = &ctx.accounts.draft_message.try_borrow_data()?;
        require!(
            msg_acc_data[..PostedMessageV1::PAYLOAD_START] == [0; PostedMessageV1::PAYLOAD_START],
            CoreBridgeError::AccountNotZeroed
        );
```

**File:** target_chains/solana/programs/core-bridge/src/processor/post_message/init_message_v1.rs (L116-119)
```rust
        PostedMessageV1Info {
            consistency_level: commitment.into(),
            emitter_authority: ctx.accounts.emitter_authority.key(),
            status: MessageStatus::Writing,
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/state/posted_message_v1/mod.rs (L139-143)
```rust
        require_keys_eq!(
            Self::emitter_authority_unsafe(&data),
            emitter_authority.key(),
            CoreBridgeError::EmitterAuthorityMismatch
        );
```

**File:** target_chains/solana/programs/core-bridge/src/processor/post_message/close_message_v1.rs (L10-14)
```rust
    #[account(
        mut,
        owner = crate::ID,
        constraint = PostedMessageV1::require_draft_message(&draft_message, &emitter_authority)?
    )]
```

**File:** target_chains/solana/programs/core-bridge/src/sdk/prepare_message.rs (L22-25)
```rust
///
/// NOTE: When using this SDK method, be aware that the message account is not created yet. You must
/// either invoke [create_account](crate::sdk::cpi::system_program::create_account) or use Anchor's
/// `init` macro directive before calling this method.
```
