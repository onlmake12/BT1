### Title
Front-Running `init_encoded_vaa` Allows Attacker to Hijack Relayer's Pre-funded Account and Steal Rent — (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs`)

---

### Summary

`init_encoded_vaa` accepts any signer as `write_authority` with no binding to the account creator. An attacker who observes a freshly created, zero-filled account owned by the core-bridge program can call `init_encoded_vaa` first, writing their own key as `write_authority`. The legitimate relayer's subsequent call then fails with `AccountNotZeroed`, and because `close_encoded_vaa` enforces `write_authority` ownership, the relayer can never reclaim the rent SOL from that account.

---

### Finding Description

The `InitEncodedVaa` account struct imposes only two constraints on `encoded_vaa`: [1](#0-0) 

- `mut` — the account is writable
- `owner = crate::ID` — the account is owned by the core-bridge program

There is no constraint that `write_authority` is the account's rent-payer, creator, or has any pre-existing relationship to `encoded_vaa`. Any signer can supply any core-bridge-owned account.

The initialization guard checks that the first `VAA_START` bytes are all zeros: [2](#0-1) 

Once `init_encoded_vaa` runs, it writes the discriminator, a `Header` (containing the caller's `write_authority` pubkey), and the VAA length into those bytes: [3](#0-2) 

This makes the zeroed-header check permanently fail for any subsequent caller, including the legitimate relayer.

`close_encoded_vaa` enforces that only the stored `write_authority` can close the account and recover rent: [4](#0-3) 

So after the attacker front-runs, the relayer's SOL is locked in the account and only the attacker can retrieve it.

---

### Impact Explanation

- **Rent theft**: The relayer pays to create and fund the account; the attacker calls `init_encoded_vaa` first, becomes the sole `write_authority`, and can call `close_encoded_vaa` to drain the rent back to themselves.
- **Sustained DoS**: The attacker can repeat this for every new account the relayer creates, continuously draining SOL and blocking VAA relay.
- **No recovery path**: The relayer cannot call `close_encoded_vaa` (wrong `write_authority`) and cannot re-initialize the account (zeroed-header check fails permanently).

---

### Likelihood Explanation

Solana transactions are observable before finalization. A bot watching for `system_program::create_account` calls that assign ownership to the core-bridge program ID can reliably front-run `init_encoded_vaa` with a higher priority fee. The attack requires no privileged access — only a funded keypair and knowledge of the target account address.

---

### Recommendation

Bind `write_authority` to the account at creation time. The standard Anchor pattern is to use a PDA derived from the `write_authority` pubkey (or to record the payer in the account and verify it on init). Concretely:

- Add a `seeds`/`bump` constraint so `encoded_vaa` is a PDA derived from `write_authority`, making it impossible for a different signer to claim the same address.
- Or: record the expected `write_authority` in the account at `create_account` time (e.g., as the first 32 bytes) and verify it matches the signer in `init_encoded_vaa`.

---

### Proof of Concept

```rust
// 1. Relayer creates account A (owned by core-bridge, all zeros, size > VAA_START)
let account_a = Keypair::new();
system_program::create_account(relayer, account_a, rent, size, core_bridge_id);

// 2. Attacker front-runs with their own write_authority
let attacker = Keypair::new();
core_bridge::init_encoded_vaa(
    Context { write_authority: attacker, encoded_vaa: account_a }
); // succeeds — account_a header now contains attacker's pubkey

// 3. Relayer's call fails
core_bridge::init_encoded_vaa(
    Context { write_authority: relayer, encoded_vaa: account_a }
); // fails: AccountNotZeroed

// 4. Attacker recovers rent; relayer's SOL is gone
core_bridge::close_encoded_vaa(
    Context { write_authority: attacker, encoded_vaa: account_a }
); // succeeds — lamports returned to attacker
```

### Citations

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs (L16-21)
```rust
    #[account(
        mut,
        owner = crate::ID
    )]
    encoded_vaa: UncheckedAccount<'info>,
}
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs (L36-40)
```rust
        let msg_acc_data: &[_] = &ctx.accounts.encoded_vaa.try_borrow_data()?;
        require!(
            msg_acc_data[..EncodedVaa::VAA_START] == [0; EncodedVaa::VAA_START],
            CoreBridgeError::AccountNotZeroed
        );
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs (L56-66)
```rust
    writer.write_all(<EncodedVaa as anchor_lang::Discriminator>::DISCRIMINATOR)?;
    (
        Header {
            status: ProcessingStatus::Writing,
            write_authority: ctx.accounts.write_authority.key(),
            version: Default::default(),
        },
        u32::try_from(vaa_len).unwrap(),
    )
        .serialize(&mut writer)
        .map_err(Into::into)
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_encoded_vaa.rs (L30-34)
```rust
        require_keys_eq!(
            EncodedVaa::write_authority_unsafe(&acc_data),
            ctx.accounts.write_authority.key(),
            CoreBridgeError::WriteAuthorityMismatch
        );
```
