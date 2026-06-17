### Title
Unprivileged Front-Run on `init_encoded_vaa` Allows Attacker to Steal Victim's Rent Lamports via `close_encoded_vaa` - (`target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs`)

---

### Summary

`init_encoded_vaa` accepts any signer as `write_authority` with no check that the signer is the account's rent payer. Because `create_account` and `init_encoded_vaa` are sent as separate transactions (as shown in both the CLI and test code), an attacker can observe the `create_account` transaction, front-run the `init_encoded_vaa` call with their own key as `write_authority`, and then immediately call `close_encoded_vaa` to drain the victim's rent lamports to themselves.

---

### Finding Description

`InitEncodedVaa` has two constraints on the `encoded_vaa` account:

1. It must be owned by `crate::ID`
2. Its header bytes must be all-zero (freshly created) [1](#0-0) [2](#0-1) 

There is **no constraint** that `write_authority` must be the account creator or rent payer. Any signer can supply their own key: [3](#0-2) 

`close_encoded_vaa` then transfers all lamports to whoever is stored as `write_authority`: [4](#0-3) [5](#0-4) 

The `close_account` utility unconditionally moves all lamports from `encoded_vaa` to `write_authority` (the attacker).

The protocol design explicitly separates `create_account` from `init_encoded_vaa` across transactions. The CLI helper returns them as separate instructions that callers batch into separate transactions: [6](#0-5) 

The test suite confirms this two-transaction pattern (TX1: create, TX2: init): [7](#0-6) 

---

### Impact Explanation

The attacker receives all lamports stored in the `encoded_vaa` account. VAA accounts hold rent for their full data size (discriminator + header + VAA buffer). For a typical Wormhole VAA (~100–500 bytes), this is roughly 0.002–0.005 SOL per account. At scale (automated relayers processing many VAAs), repeated exploitation drains meaningful funds from relayers.

---

### Likelihood Explanation

Solana does not have a public mempool in the Ethereum sense, but:
- RPC nodes see transactions before finalization
- Validators and co-located bots can observe and reorder within a slot
- The attack window is the gap between the `create_account` transaction landing and the `init_encoded_vaa` transaction landing — two separate on-chain transactions

Any party running an RPC node or co-located with a validator can reliably exploit this. The attack requires no privileged access, no leaked keys, and no governance control.

---

### Recommendation

Require that `write_authority` is also a signer on the `create_account` instruction, or — more practically — add a constraint in `InitEncodedVaa` that the `write_authority` must equal the `encoded_vaa` account's **rent payer** by passing the payer as an additional account and verifying it matches. Alternatively, combine `create_account` and `init_encoded_vaa` into a single atomic CPI within the program so the two steps cannot be split across transactions.

---

### Proof of Concept

```
1. Keypair A (victim) calls system_instruction::create_account:
   - payer = A
   - new_account = encoded_vaa_keypair
   - owner = crate::ID
   - lamports = rent_minimum_balance(vaa_size)
   → Transaction 1 lands; encoded_vaa is owned by crate::ID, all-zero header

2. Attacker (keypair B) observes Transaction 1 in the RPC/mempool,
   submits before victim's Transaction 2:
   init_encoded_vaa {
       write_authority: B,   // attacker's key
       encoded_vaa: encoded_vaa_keypair.pubkey(),
   }
   → Passes: owner == crate::ID ✓, header all-zero ✓
   → Writes write_authority = B into account header

3. Attacker calls:
   close_encoded_vaa {
       write_authority: B,
       encoded_vaa: encoded_vaa_keypair.pubkey(),
   }
   → Passes: discriminator ✓, write_authority matches B ✓
   → close_account transfers all lamports to B

Result: B's balance increases by rent_minimum_balance(vaa_size);
        A's balance decreased by the same amount with nothing to show for it.
```

### Citations

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs (L16-20)
```rust
    #[account(
        mut,
        owner = crate::ID
    )]
    encoded_vaa: UncheckedAccount<'info>,
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs (L36-40)
```rust
        let msg_acc_data: &[_] = &ctx.accounts.encoded_vaa.try_borrow_data()?;
        require!(
            msg_acc_data[..EncodedVaa::VAA_START] == [0; EncodedVaa::VAA_START],
            CoreBridgeError::AccountNotZeroed
        );
```

**File:** target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs (L57-62)
```rust
    (
        Header {
            status: ProcessingStatus::Writing,
            write_authority: ctx.accounts.write_authority.key(),
            version: Default::default(),
        },
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

**File:** target_chains/solana/programs/pyth-solana-receiver/tests/test_post_updates_with_wormhole.rs (L119-135)
```rust
    // TX2: Init encoded VAA
    program_simulator
        .process_ix_with_default_compute_limit(
            Instruction {
                program_id: BRIDGE_ID,
                accounts: wormhole_core_bridge_solana::accounts::InitEncodedVaa {
                    write_authority: write_authority.pubkey(),
                    encoded_vaa: encoded_vaa_keypair.pubkey(),
                }
                .to_account_metas(None),
                data: wormhole_core_bridge_solana::instruction::InitEncodedVaa.data(),
            },
            &vec![],
            Some(&write_authority),
        )
        .await
        .unwrap();
```
