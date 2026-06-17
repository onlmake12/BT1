### Title
PDA Pre-funding Griefing Permanently Blocks Publisher Initialization — (`target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs`)

### Summary

An unprivileged attacker can permanently prevent any publisher from being initialized by sending 1 lamport to the deterministic `publisher_config` PDA address before the authority calls `InitializePublisher`. Solana's system program rejects `create_account` when the destination account already has a non-zero lamport balance, and the program has no alternative initialization path.

### Finding Description

`initialize_publisher` unconditionally calls `system_instruction::create_account` to allocate the `publisher_config` PDA: [1](#0-0) 

`validate_publisher_config_for_init` only verifies the PDA derivation, bump correctness, and writability — it does **not** check whether the account already holds lamports: [2](#0-1) 

Solana's system program enforces: if the destination account has `lamports > 0`, `create_account` returns `SystemError::AccountAlreadyInUse` (error 0). Because the PDA address is derived deterministically from `[PUBLISHER_CONFIG_SEED, publisher_bytes]` and `program_id` — both public — any observer can compute it off-chain and pre-fund it with 1 lamport before the authority acts. [3](#0-2) 

The program contains no instruction to drain lamports from an uninitialized PDA, no `allocate`+`assign` fallback path, and no re-initialization mechanism. The comment in the source explicitly states the publisher config can only be set once: [4](#0-3) 

### Impact Explanation

The `publisher_config` PDA address is immutable and unique per publisher. Once pre-funded, the authority's `InitializePublisher` transaction will always fail. The publisher can never submit prices (the `submit_prices` path requires a valid `publisher_config`). If the targeted publisher is critical to Pyth's price aggregation quorum, this causes stale or unavailable price feeds — a protocol-level availability failure. Recovery requires a program upgrade, which introduces operational delay and is not guaranteed.

### Likelihood Explanation

The attack requires no privileged access, no leaked keys, and costs 1 lamport (~$0.000001). Both inputs needed to compute the PDA (`program_id` and `publisher` pubkey) are public on-chain. The attacker only needs to act before the authority calls `InitializePublisher` for a given publisher — a window that exists for every new publisher onboarding event.

### Recommendation

Replace the unconditional `create_account` call with a pattern that handles pre-existing lamports:

1. Check `publisher_config.lamports()` before calling `create_account`.
2. If lamports are already present, use `system_instruction::allocate` + `system_instruction::assign` (via `invoke_signed`) and top up only the deficit, instead of `create_account`.
3. Alternatively, add a guard in `validate_publisher_config_for_init` that rejects accounts with non-zero lamports and non-zero data length, while allowing the "lamports only, no data, system-owned" case to proceed via the allocate/assign path.

The same pattern applies to `initialize.rs` for the config PDA. [5](#0-4) 

### Proof of Concept

```rust
#[tokio::test]
async fn test_griefing_blocks_publisher_init() {
    let id = Pubkey::new_unique();
    let (mut banks_client, authority, recent_blockhash) = ProgramTest::new(
        "publishers", id,
        processor!(crate::processor::process_instruction),
    ).start().await;

    // 1. Initialize program config (normal flow)
    // ... (omitted for brevity, same as existing test)

    let publisher = Keypair::new();
    let (publisher_config_pda, publisher_config_bump) = Pubkey::find_program_address(
        &[PUBLISHER_CONFIG_SEED.as_bytes(), &publisher.pubkey().to_bytes()],
        &id,
    );

    // 2. Attacker pre-funds the PDA with 1 lamport
    let attacker = Keypair::new();
    // fund attacker first, then:
    let tx = Transaction::new_with_payer(
        &[system_instruction::transfer(&attacker.pubkey(), &publisher_config_pda, 1)],
        Some(&attacker.pubkey()),
    );
    banks_client.process_transaction(tx).await.unwrap();

    // 3. Authority calls InitializePublisher — must fail
    let mut data = vec![
        crate::instruction::Instruction::InitializePublisher as u8,
        config_bump, publisher_config_bump,
    ];
    data.extend_from_slice(&publisher.pubkey().to_bytes());
    let tx = Transaction::new_with_payer(
        &[Instruction { program_id: id, data, accounts: vec![...] }],
        Some(&authority.pubkey()),
    );
    // Asserts the transaction fails with AccountAlreadyInUse
    assert!(banks_client.process_transaction(tx).await.is_err());

    // 4. No recovery path: PDA address is fixed, program has no drain instruction
}
```

### Citations

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs (L29-30)
```rust
/// a single buffer key. If we need to modify the buffer key or create multiple buffer keys
/// per publisher, we'll need to upgrade the program.
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs (L53-69)
```rust
    invoke_signed(
        &system_instruction::create_account(
            authority.key,
            publisher_config.key,
            lamports,
            publisher_config::SIZE
                .try_into()
                .expect("unexpected overflow"),
            program_id,
        ),
        &[authority.clone(), publisher_config.clone(), system.clone()],
        &[&[
            PUBLISHER_CONFIG_SEED.as_bytes(),
            &args.publisher,
            &[args.publisher_config_bump],
        ]],
    )?;
```

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L109-129)
```rust
pub fn validate_publisher_config_for_init<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
    bump: u8,
    publisher: &Pubkey,
    program_id: &Pubkey,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let publisher_config = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    // We use `find_program_address` to guarantee that only one publisher_config
    // is created per publisher.
    let (publisher_config_pda, expected_bump) = Pubkey::find_program_address(
        &[PUBLISHER_CONFIG_SEED.as_bytes(), &publisher.to_bytes()],
        program_id,
    );
    ensure!(ProgramError::InvalidInstructionData, bump == expected_bump);
    ensure!(ProgramError::InvalidArgument, publisher_config.is_writable);
    ensure!(
        ProgramError::MissingRequiredSignature,
        pubkey_eq(publisher_config.key, &publisher_config_pda)
    );
    Ok(publisher_config)
}
```

**File:** target_chains/solana/programs/pyth-price-store/src/instruction.rs (L1-10)
```rust
use bytemuck::{Pod, Zeroable};

/// Seed used to derive the config account.
pub const CONFIG_SEED: &str = "CONFIG";

/// Seed used to derive the associated buffer account that publishers can
/// write their updates into.
pub const PUBLISHER_CONFIG_SEED: &str = "PUBLISHER_CONFIG";

#[repr(u8)]
```

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L29-41)
```rust
    invoke_signed(
        &system_instruction::create_account(
            payer.key,
            config.key,
            lamports,
            accounts::config::SIZE
                .try_into()
                .expect("unexpected overflow"),
            program_id,
        ),
        &[payer.clone(), config.clone(), system.clone()],
        &[&[CONFIG_SEED.as_bytes(), &[args.config_bump]]],
    )?;
```
