### Title
Front-running `init_encoded_vaa` allows attacker to hijack `EncodedVaa` account and steal relayer's rent deposit — (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs`)

---

### Summary

`init_encoded_vaa` accepts any signer as `write_authority` with no check that the signer is the one who funded the `encoded_vaa` account. An attacker who observes a relayer's `system_program::create_account` in the mempool can front-run with their own `init_encoded_vaa`, claim `write_authority`, and then call `close_encoded_vaa` to drain all lamports the relayer deposited.

---

### Finding Description

The `InitEncodedVaa` account struct imposes only two constraints on the `encoded_vaa` account:

1. It must be owned by `crate::ID`.
2. Its first `VAA_START` bytes must be all zeros. [1](#0-0) 

The `write_authority` field is only required to be a `Signer` — there is no constraint tying it to the account's creator or funder. [2](#0-1) 

The zero-check guard only prevents double-initialization; it does not prevent a third party from being the first to call `init_encoded_vaa` on a freshly-created account: [3](#0-2) 

Once `init_encoded_vaa` succeeds, the attacker's key is written into `Header.write_authority`: [4](#0-3) 

`close_encoded_vaa` only verifies that the caller's key matches the stored `write_authority` — it has no check that the caller is the original funder: [5](#0-4) 

`close_account` unconditionally transfers all lamports to `sol_destination`, which is `write_authority` (the attacker): [6](#0-5) 

---

### Impact Explanation

The relayer loses the full rent-exempt deposit it paid to create the `encoded_vaa` account. On Solana, rent-exempt deposits for accounts sized to hold a VAA (which can be up to ~10 KB) are non-trivial. A persistent attacker can drain every relayer that uses the two-step `create_account` + `init_encoded_vaa` pattern, making relaying economically unviable.

---

### Likelihood Explanation

Solana's mempool is observable. The attack requires no privileged access, no leaked keys, and no governance majority. It only requires:
- Watching for `system_program::create_account` transactions that assign ownership to the core-bridge program ID.
- Submitting `init_encoded_vaa` with a higher priority fee before the relayer's follow-up transaction lands.

This is a standard Solana front-running pattern and is straightforwardly automatable.

---

### Recommendation

Require that `write_authority` is the same account that funded `encoded_vaa`. The cleanest fix is to record the funder's key in the account at creation time, or to require `write_authority` to be the `encoded_vaa` account's lamport source. Concretely, add a constraint in `InitEncodedVaa` that checks `write_authority` is the system-level owner/funder of the keypair, or restructure the flow so that `create_account` and `init_encoded_vaa` are atomic (i.e., use Anchor's `init` macro, which combines allocation and initialization in a single instruction and enforces the payer relationship).

---

### Proof of Concept

```
1. Relayer generates keypair K.
2. Relayer broadcasts: system_program::create_account(
       from=relayer, to=K, lamports=rent_exempt, space=N, owner=core_bridge_id)
3. Attacker observes step 2 in the mempool.
4. Attacker broadcasts (higher priority fee):
       init_encoded_vaa(write_authority=attacker, encoded_vaa=K)
   → passes: K is owned by core_bridge_id, K.data is all zeros.
   → writes Header { write_authority: attacker } into K.
5. Relayer's init_encoded_vaa(write_authority=relayer, encoded_vaa=K) fails:
   → CoreBridgeError::AccountNotZeroed (data[..VAA_START] != 0).
6. Attacker broadcasts:
       close_encoded_vaa(write_authority=attacker, encoded_vaa=K)
   → passes: discriminator matches, write_authority matches attacker.
   → close_account transfers all lamports from K to attacker.
```

Relayer's rent deposit is fully drained. Testable on a local validator with two keypairs and `solana-test-validator`.

### Citations

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs (L9-21)
```rust
#[derive(Accounts)]
pub struct InitEncodedVaa<'info> {
    /// The authority who can write to the VAA account when it is being processed.
    write_authority: Signer<'info>,

    /// CHECK: This account will have been created using the system program outside of the Core
    /// Bridge.
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

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs (L57-65)
```rust
    (
        Header {
            status: ProcessingStatus::Writing,
            write_authority: ctx.accounts.write_authority.key(),
            version: Default::default(),
        },
        u32::try_from(vaa_len).unwrap(),
    )
        .serialize(&mut writer)
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_encoded_vaa.rs (L30-34)
```rust
        require_keys_eq!(
            EncodedVaa::write_authority_unsafe(&acc_data),
            ctx.accounts.write_authority.key(),
            CoreBridgeError::WriteAuthorityMismatch
        );
```

**File:** target_chains/solana/programs/core-bridge/src/utils/mod.rs (L22-31)
```rust
pub(crate) fn close_account(info: &AccountInfo, sol_destination: &AccountInfo) -> Result<()> {
    // Transfer tokens from the account to the sol_destination.
    let dest_starting_lamports = sol_destination.lamports();
    **sol_destination.lamports.borrow_mut() =
        dest_starting_lamports.checked_add(info.lamports()).unwrap();
    **info.lamports.borrow_mut() = 0;

    info.assign(&system_program::ID);
    info.resize(0).map_err(Into::into)
}
```
