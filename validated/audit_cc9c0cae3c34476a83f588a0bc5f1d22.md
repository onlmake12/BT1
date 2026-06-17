The vulnerability is real. Here is the complete analysis:

---

### Title
Unprivileged Front-Running of `init_encoded_vaa` Allows Rent Theft and VAA Relay DoS — (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs`)

### Summary

`init_encoded_vaa` accepts any signer as `write_authority` and writes that key into the account header with no check that the signer is the account's creator/payer. An attacker who observes a victim's freshly-created, uninitialized `encoded_vaa` account can call `init_encoded_vaa` first, stamp their own key as `write_authority`, and then drain the victim's rent via `close_encoded_vaa`.

### Finding Description

The `InitEncodedVaa` account struct imposes only three constraints on the `encoded_vaa` account: [1](#0-0) 

1. `owner = crate::ID` — the account must already be owned by the core bridge program.
2. `data_len() > EncodedVaa::VAA_START` — the account must be large enough.
3. The first `VAA_START` bytes must be all zeros (not yet initialized). [2](#0-1) 

There is **no check** that `write_authority` is the payer or creator of the account. The function unconditionally writes the caller's key into the header: [3](#0-2) 

The standard relayer flow (visible in the CLI) is:
1. `system_instruction::create_account` — creates and assigns the account to the core bridge program.
2. `init_encoded_vaa` — initializes the header.
3. `write_encoded_vaa` — writes VAA bytes. [4](#0-3) 

Between steps 1 and 2, the account is owned by the core bridge and fully zeroed — satisfying all three constraints. Any observer can call `init_encoded_vaa` on it first.

### Impact Explanation

**Step 1 — Rent theft:** After the attacker initializes the account with their own `write_authority`, they call `close_encoded_vaa`. That instruction requires only that the signer matches the stored `write_authority`: [5](#0-4) 

It then transfers all lamports to the `write_authority` account: [6](#0-5) 

The victim paid the rent; the attacker receives it.

**Step 2 — VAA relay DoS:** The victim's subsequent `write_encoded_vaa` call fails because `require_draft_vaa` enforces a strict key equality between the stored `write_authority` and the transaction signer: [7](#0-6) 

The victim must create a new account and retry, paying rent again. The attacker can repeat this indefinitely.

### Likelihood Explanation

- The attack requires no privileged access — any keypair can call `init_encoded_vaa`.
- The victim's account pubkey is visible in the mempool before confirmation.
- Solana validators allow transaction reordering within a block; a higher-fee attacker transaction can reliably land first.
- The attack is cheap: two transactions (init + close) net the attacker the victim's rent minus fees.

### Recommendation

Add a constraint in `InitEncodedVaa` that ties `write_authority` to the account's funding payer. The simplest fix is to require that `write_authority` is also a signer on the `create_account` system instruction — i.e., enforce that `write_authority.key() == encoded_vaa` account's payer — or, more practically, require the `write_authority` to be the account's current lamport owner by passing the payer as an additional checked account. Alternatively, combine `create_account` and `init_encoded_vaa` into a single atomic instruction so there is no window between account creation and initialization.

### Proof of Concept

```
1. victim_keypair creates encoded_vaa_account via system_instruction::create_account,
   assigning owner = core_bridge::ID, size = VAA_START + vaa.len()

2. attacker_keypair submits init_encoded_vaa {
       write_authority: attacker_keypair,   // attacker signs
       encoded_vaa:     encoded_vaa_account // victim's account
   } with higher priority fee → lands before victim's tx

3. Header is now: { write_authority: attacker_pubkey, status: Writing, ... }

4. victim_keypair submits write_encoded_vaa {
       write_authority: victim_keypair,
       draft_vaa:       encoded_vaa_account
   } → fails with WriteAuthorityMismatch

5. attacker_keypair submits close_encoded_vaa {
       write_authority: attacker_keypair,
       encoded_vaa:     encoded_vaa_account
   } → succeeds; victim's rent transferred to attacker_keypair

Assert: victim lamport balance decreased by rent_exempt_minimum(VAA_START + vaa.len())
Assert: attacker lamport balance increased by same amount (minus tx fees)
```

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

**File:** target_chains/solana/cli/src/main.rs (L767-809)
```rust
    let create_encoded_vaa = system_instruction::create_account(
        payer,
        &encoded_vaa_keypair.pubkey(),
        Rent::default().minimum_balance(encoded_vaa_size),
        encoded_vaa_size as u64,
        wormhole,
    );

    let init_encoded_vaa_accounts = wormhole_core_bridge_solana::accounts::InitEncodedVaa {
        write_authority: *payer,
        encoded_vaa: encoded_vaa_keypair.pubkey(),
    }
    .to_account_metas(None);

    let init_encoded_vaa_instruction = Instruction {
        program_id: *wormhole,
        accounts: init_encoded_vaa_accounts,
        data: wormhole_core_bridge_solana::instruction::InitEncodedVaa.data(),
    };

    let write_encoded_vaa_accounts = wormhole_core_bridge_solana::accounts::WriteEncodedVaa {
        write_authority: *payer,
        draft_vaa: encoded_vaa_keypair.pubkey(),
    }
    .to_account_metas(None);

    let write_encoded_vaa_instruction = Instruction {
        program_id: *wormhole,
        accounts: write_encoded_vaa_accounts,
        data: wormhole_core_bridge_solana::instruction::WriteEncodedVaa {
            args: WriteEncodedVaaArgs {
                index: 0,
                data: vaa[..VAA_SPLIT_INDEX].to_vec(),
            },
        }
        .data(),
    };

    Ok(vec![
        create_encoded_vaa,
        init_encoded_vaa_instruction,
        write_encoded_vaa_instruction,
    ])
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_encoded_vaa.rs (L30-34)
```rust
        require_keys_eq!(
            EncodedVaa::write_authority_unsafe(&acc_data),
            ctx.accounts.write_authority.key(),
            CoreBridgeError::WriteAuthorityMismatch
        );
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_encoded_vaa.rs (L42-43)
```rust
pub fn close_encoded_vaa(ctx: Context<CloseEncodedVaa>) -> Result<()> {
    crate::utils::close_account(&ctx.accounts.encoded_vaa, &ctx.accounts.write_authority)
```

**File:** target_chains/solana/programs/core-bridge/src/state/encoded_vaa.rs (L105-109)
```rust
        require_keys_eq!(
            Self::write_authority_unsafe(&data),
            write_authority.key(),
            CoreBridgeError::WriteAuthorityMismatch
        );
```
