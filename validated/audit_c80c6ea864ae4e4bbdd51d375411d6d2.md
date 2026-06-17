### Title
Improper PDA Validation in `validate_buffer` Allows Writing Price Data to Arbitrary Program-Owned Accounts — (File: `target_chains/solana/programs/pyth-price-store/src/validate.rs`)

---

### Summary

The `validate_buffer` function in `pyth-price-store` fails to validate the PDA seeds of the buffer account. It only checks `is_writable` and `owner == program_id`, meaning any account owned by the program can be passed as the buffer in a `submit_prices` call. This is a direct structural analog to the reported `expand_sandwich_validators_bitmap` issue: an instruction account context accepts a PDA without deriving or constraining it by its seeds.

---

### Finding Description

In `target_chains/solana/programs/pyth-price-store/src/validate.rs`, the `validate_buffer` function is:

```rust
pub fn validate_buffer<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
    program_id: &Pubkey,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let buffer = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    ensure!(ProgramError::InvalidArgument, buffer.is_writable);
    ensure!(ProgramError::IllegalOwner, buffer.owner == program_id);
    Ok(buffer)
}
```

The buffer PDA is expected to be derived from seeds `[allowed_program_auth, MESSAGE, base_account_key]` (as seen in `create_buffer.rs` and `put_all.rs`). However, `validate_buffer` performs **no seed derivation or address check**. It accepts any account that is:
- writable, and
- owned by the program.

This means a publisher calling `submit_prices` can supply:
- Another publisher's buffer account (different `base_account_key`)
- The global `config` PDA (seeds `[CONFIG_SEED]`)
- Another publisher's `publisher_config` PDA (seeds `[PUBLISHER_CONFIG_SEED, publisher_key]`)

All of these are owned by the program and can be marked writable by the transaction sender.

By contrast, all other account validations in the same file (`validate_config`, `validate_publisher_config_for_init`, `validate_publisher_config_for_access`) correctly derive the expected PDA address and compare it against the supplied account key before accepting it.

---

### Impact Explanation

A publisher can direct `submit_prices` to write raw price data bytes into an arbitrary program-owned account. Depending on which account is targeted:

- **Config account**: Overwriting it corrupts the stored `authority` public key, permanently locking out the program authority from governance operations.
- **Another publisher's `publisher_config`**: Corrupts that publisher's stored configuration, preventing them from submitting prices or being re-initialized without admin intervention.
- **Another publisher's buffer**: Silently overwrites their price data with the attacker's data, causing incorrect price feeds to be published on-chain.

**Impact: Medium–High** (data corruption of critical program state, potential permanent DoS of authority or other publishers).

---

### Likelihood Explanation

Any authorized publisher can trigger this. The `submit_prices` instruction is a normal operational path called frequently. The publisher only needs to pass a different account key in the buffer slot of the transaction — no special tooling or key compromise is required. **Likelihood: High.**

---

### Recommendation

Derive and verify the buffer PDA inside `validate_buffer` using the same seeds used in `create_buffer`:

```rust
pub fn validate_buffer<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
    allowed_program_auth: &Pubkey,
    base_account_key: &Pubkey,
    program_id: &Pubkey,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let buffer = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    let (expected_pda, _) = Pubkey::find_program_address(
        &[
            allowed_program_auth.as_ref(),
            MESSAGE.as_bytes(),
            base_account_key.as_ref(),
        ],
        program_id,
    );
    ensure!(ProgramError::InvalidArgument, buffer.is_writable);
    ensure!(ProgramError::IllegalOwner, buffer.owner == program_id);
    ensure!(ProgramError::InvalidArgument, pubkey_eq(buffer.key, &expected_pda));
    Ok(buffer)
}
```

---

### Proof of Concept

1. Publisher `P` is legitimately authorized and holds a valid `publisher_config` PDA.
2. `P` constructs a `submit_prices` transaction where the buffer account slot is set to the program's `config` PDA address (seeds `[CONFIG_SEED]`), marked writable.
3. `validate_buffer` is called: `config.is_writable == true` ✓, `config.owner == program_id` ✓ — validation passes.
4. `submit_prices` writes raw price bytes into the `config` account's data region.
5. The `authority` field stored in `config` is overwritten with price data bytes.
6. All subsequent calls that read `config.authority` (e.g., `validate_authority`) now compare against garbage bytes, permanently locking out the legitimate authority.

**Root cause**: [1](#0-0) 

**Contrast with correct PDA validation** (config and publisher_config both derive and compare): [2](#0-1) 

**Expected seeds from `create_buffer`**: [3](#0-2) 

**Same seeds enforced correctly in `put_all` and `delete_buffer`**: [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L46-64)
```rust
pub fn validate_config<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
    bump: u8,
    program_id: &Pubkey,
    require_writable: bool,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let config = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    let (config_pda, expected_bump) =
        Pubkey::find_program_address(&[CONFIG_SEED.as_bytes()], program_id);
    ensure!(ProgramError::InvalidInstructionData, bump == expected_bump);
    ensure!(
        ProgramError::InvalidArgument,
        pubkey_eq(config.key, &config_pda)
    );
    if require_writable {
        ensure!(ProgramError::InvalidArgument, config.is_writable);
    }
    Ok(config)
}
```

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L131-139)
```rust
pub fn validate_buffer<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
    program_id: &Pubkey,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let buffer = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    ensure!(ProgramError::InvalidArgument, buffer.is_writable);
    ensure!(ProgramError::IllegalOwner, buffer.owner == program_id);
    Ok(buffer)
}
```

**File:** pythnet/message_buffer/programs/message_buffer/src/instructions/create_buffer.rs (L41-49)
```rust
        let (pda, bump) = Pubkey::find_program_address(
            &[
                allowed_program_auth.as_ref(),
                MESSAGE.as_bytes(),
                base_account_key.as_ref(),
            ],
            &crate::ID,
        );
        require_keys_eq!(buffer_account.key(), pda);
```

**File:** pythnet/message_buffer/programs/message_buffer/src/instructions/put_all.rs (L33-38)
```rust
    #[account(
        mut,
        seeds = [whitelist_verifier.cpi_caller_auth.key().as_ref(), MESSAGE.as_bytes(), base_account_key.as_ref()],
        bump = message_buffer.load()?.bump,
    )]
    pub message_buffer: AccountLoader<'info, MessageBuffer>,
```

**File:** pythnet/message_buffer/programs/message_buffer/src/instructions/delete_buffer.rs (L30-36)
```rust
    #[account(
        mut,
        close = payer,
        seeds = [allowed_program_auth.as_ref(), MESSAGE.as_bytes(), base_account_key.as_ref()],
        bump = message_buffer.load()?.bump,
    )]
    pub message_buffer: AccountLoader<'info, MessageBuffer>,
```
