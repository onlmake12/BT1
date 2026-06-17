Looking at the actual code to verify the claim precisely.

### Title
Griefing via Pre-Funded Sequence PDA Permanently Blocks PDA-Payer Emitters from Posting Messages — (`target_chains/solana/programs/core-bridge/src/utils/cpi.rs`)

---

### Summary

`allocate_and_assign_account` uses `CpiContext::new` (no signer seeds) for the payer→new_account `system_program::transfer`. When the payer is a PDA authorized only via `invoke_signed` in a parent CPI, the System program rejects the transfer because the PDA is not a transaction-level signer and no seeds are forwarded. An attacker can force this code path by pre-funding the emitter sequence PDA with 1 lamport before the first `post_message` call, permanently blocking that emitter.

---

### Finding Description

**Step 1 — The branch condition.**
`create_account_safe` checks `current_lamports` on the target account. If `> 0`, it skips `create_account` and calls `allocate_and_assign_account` instead. [1](#0-0) 

**Step 2 — The missing signer seeds in the transfer.**
Inside `allocate_and_assign_account`, the rent top-up transfer uses `CpiContext::new` — no signer seeds at all. The `ctx.signer_seeds` that *are* available in this function are the seeds for the **emitter_sequence** PDA (passed from the caller), not the payer's seeds. [2](#0-1) 

Contrast with `allocate` and `assign` immediately below, which correctly use `CpiContext::new_with_signer(…, ctx.signer_seeds)` — those work because `ctx.signer_seeds` are the emitter_sequence PDA seeds, which is the account being allocated/assigned. [3](#0-2) 

**Step 3 — How `create_account_safe` is called for the sequence account.**
`create_or_realloc_emitter_sequence` calls `create_account_safe` via `CpiContext::new_with_signer` where the signer seeds are the emitter_sequence PDA seeds — not the payer's seeds. There is no mechanism to pass payer seeds through. [4](#0-3) 

**Step 4 — The payer constraint.**
`PostMessage` declares `payer: Signer<'info>`. Anchor's `Signer` only checks `is_signer == true`. When an integrating program CPIs into `post_message` via `invoke_signed` with PDA seeds, the PDA's `is_signer` flag is `true` inside `post_message`, so Anchor's constraint passes. But when `post_message` then does a nested `invoke` (not `invoke_signed`) to the System program, Solana's runtime does **not** propagate the PDA's signer status. The System program rejects the transfer. [5](#0-4) 

**Step 5 — The same `CpiContext::new` pattern also appears in the legacy-migration branch** (realloc of old 8-byte sequence accounts), creating an identical failure mode there. [6](#0-5) 

---

### Impact Explanation

An attacker sends 1 lamport to the emitter sequence PDA in a separate transaction (trivially cheap). Every subsequent `post_message` call from an integrator that uses a PDA payer enters `allocate_and_assign_account`, hits the System program transfer, and fails. The 1 lamport persists across failed transactions (it was deposited in a prior, successful transaction), so the block is permanent until the sequence PDA is somehow drained — which is non-trivial because the account is not yet owned by the core bridge program and has no data.

---

### Likelihood Explanation

The precondition is that the integrating program uses a PDA as the payer. The SDK's `PublishMessage` struct declares `payer: UncheckedAccount<'info>` (no `Signer` enforcement at the SDK layer), which explicitly supports PDA payers. Any program that CPIs `post_message` with a PDA payer — a common pattern for programs that hold SOL in a treasury PDA — is vulnerable. The attacker cost is 1 lamport plus one transaction fee. [7](#0-6) 

---

### Recommendation

Change `allocate_and_assign_account` to accept an optional `payer_seeds: &[&[&[u8]]]` parameter and use `CpiContext::new_with_signer` for the transfer when seeds are provided. Propagate payer seeds from `create_account_safe` through to `allocate_and_assign_account`. Apply the same fix to the legacy-migration transfer in `create_or_realloc_emitter_sequence`.

---

### Proof of Concept

1. Deploy an integrating program whose `post_message` handler uses a PDA (e.g., seeds `[b"treasury"]`) as the payer.
2. Derive the emitter sequence PDA: `[b"Sequence", emitter_pubkey]`.
3. Send 1 lamport to the sequence PDA from any wallet.
4. Call the integrating program's instruction that CPIs `post_message`.
5. Observe the transaction fails with a System program error on the transfer inside `allocate_and_assign_account`.
6. Repeat step 4 indefinitely — every call fails. The emitter can never post a message.

### Citations

**File:** target_chains/solana/programs/core-bridge/src/utils/cpi.rs (L54-71)
```rust
    let current_lamports = ctx.accounts.new_account.lamports();
    if current_lamports == 0 {
        system_program::create_account(
            CpiContext::new_with_signer(
                ctx.program_id,
                system_program::CreateAccount {
                    from: ctx.accounts.payer,
                    to: ctx.accounts.new_account,
                },
                ctx.signer_seeds,
            ),
            Rent::get().map(|rent| rent.minimum_balance(data_len))?,
            data_len.try_into().unwrap(),
            owner,
        )
    } else {
        allocate_and_assign_account(ctx, data_len, owner, current_lamports)
    }
```

**File:** target_chains/solana/programs/core-bridge/src/utils/cpi.rs (L85-96)
```rust
    if required_lamports > 0 {
        system_program::transfer(
            CpiContext::new(
                ctx.program_id,
                system_program::Transfer {
                    from: ctx.accounts.payer,
                    to: ctx.accounts.new_account.to_account_info(),
                },
            ),
            required_lamports,
        )?;
    }
```

**File:** target_chains/solana/programs/core-bridge/src/utils/cpi.rs (L99-120)
```rust
    system_program::allocate(
        CpiContext::new_with_signer(
            ctx.program_id,
            system_program::Allocate {
                account_to_allocate: ctx.accounts.new_account.to_account_info(),
            },
            ctx.signer_seeds,
        ),
        data_len.try_into().unwrap(),
    )?;

    // Assign to the owner.
    system_program::assign(
        CpiContext::new_with_signer(
            ctx.program_id,
            system_program::Assign {
                account_to_assign: ctx.accounts.new_account.to_account_info(),
            },
            ctx.signer_seeds,
        ),
        owner,
    )
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/mod.rs (L74-79)
```rust
    /// Payer (mut signer).
    ///
    /// This account pays for new accounts created and pays for the Wormhole fee.
    #[account(mut)]
    payer: Signer<'info>,

```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/mod.rs (L378-393)
```rust
        utils::cpi::create_account_safe(
            CpiContext::new_with_signer(
                system_program.key(),
                utils::cpi::CreateAccountSafe {
                    payer: payer.to_account_info(),
                    new_account: emitter_sequence.to_account_info(),
                },
                &[&[
                    EmitterSequence::SEED_PREFIX,
                    emitter.as_ref(),
                    &[emitter_sequence_bump],
                ]],
            ),
            EmitterSequence::INIT_SPACE,
            &crate::ID,
        )?;
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/mod.rs (L413-422)
```rust
        system_program::transfer(
            CpiContext::new(
                system_program.key(),
                system_program::Transfer {
                    from: payer.to_account_info(),
                    to: emitter_sequence.to_account_info(),
                },
            ),
            lamports_diff,
        )?;
```

**File:** target_chains/solana/programs/core-bridge/src/sdk/publish_message.rs (L12-18)
```rust
pub struct PublishMessage<'info> {
    /// Payer (mut signer).
    ///
    /// CHECK: This account's lamports will be used to create various accounts when publishing a
    /// Wormhole message.
    pub payer: UncheckedAccount<'info>,

```
