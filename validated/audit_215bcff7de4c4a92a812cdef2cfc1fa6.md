### Title
Unprivileged Attacker Can Permanently Brick the Price-Store Program via Zero-Authority Initialization — (`target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs`)

---

### Summary

The `Initialize` instruction accepts an arbitrary `authority` pubkey with no validation. Any unprivileged caller can front-run the legitimate deployer, pass `authority = [0u8; 32]` (the zero pubkey), and permanently prevent any publisher from ever being registered, because `InitializePublisher` requires a signer whose key matches the stored authority — and no keypair exists for the zero pubkey.

---

### Finding Description

The `initialize` function stores whatever `args.authority` is passed directly into the config PDA without any non-zero or validity check: [1](#0-0) 

The `accounts::config::create` function only guards against re-initialization (non-zero `format`), not against a zero authority: [2](#0-1) 

Once the config PDA is created, it cannot be re-initialized — `create` returns `AlreadyInitialized` if `data.format != 0`: [3](#0-2) 

`InitializePublisher` calls `validate_authority`, which requires the first account to be a **signer** whose key matches `config.authority`: [4](#0-3) 

If `config.authority` is `[0u8; 32]`, the signer check at line 71 and the key-equality check at line 77 together require a signature from the zero pubkey — for which no private key exists. The transaction will always fail with `MissingRequiredSignature`.

There are only three instructions in the program (`Initialize`, `SubmitPrices`, `InitializePublisher`) and no `UpdateAuthority` or admin-recovery path: [5](#0-4) 

---

### Impact Explanation

Permanent denial-of-service on the price-store program. No publisher can ever be registered via `InitializePublisher`, so no price data can be submitted. The config PDA is immutable once created, and there is no upgrade or recovery instruction.

---

### Likelihood Explanation

The attack requires only:
1. Knowledge of the program ID (public after deployment).
2. Enough SOL to pay rent for the config PDA (~0.001 SOL).
3. Submitting the `Initialize` transaction before the legitimate deployer — a straightforward front-run on Solana's public mempool, or simply racing during the deployment window.

No privileged access, leaked keys, or governance majority is needed.

---

### Recommendation

Add a non-zero authority check inside `initialize` before writing to the config account:

```rust
// In processor/initialize.rs, before accounts::config::create(...)
ensure!(
    ProgramError::InvalidArgument,
    args.authority != [0u8; 32]
);
```

Additionally, consider requiring the `payer` (or a designated deployer account) to match a hard-coded or program-derived expected authority, so that only the legitimate deployer can call `Initialize`.

---

### Proof of Concept

```rust
// 1. Attacker calls Initialize with zero authority
let mut data = vec![Instruction::Initialize as u8, config_bump];
data.extend_from_slice(&[0u8; 32]); // zero authority
// ... submit transaction (any signer can be payer) ...

// 2. Legitimate deployer's Initialize now fails: AlreadyInitialized
// config.authority == [0u8; 32]

// 3. Any attempt at InitializePublisher fails:
// validate_authority checks authority.is_signer && authority.key == [0u8;32]
// => MissingRequiredSignature (zero pubkey cannot sign)

// 4. No recovery path: no UpdateAuthority instruction exists.
assert!(initialize_publisher_result == Err(ProgramError::MissingRequiredSignature));
```

### Citations

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L43-43)
```rust
    accounts::config::create(*config.data.borrow_mut(), args.authority)?;
```

**File:** target_chains/solana/programs/pyth-price-store/src/accounts/config.rs (L38-48)
```rust
pub fn create(data: &mut [u8], authority: [u8; 32]) -> Result<&mut Config, ReadAccountError> {
    if data.len() < size_of::<Config>() {
        return Err(ReadAccountError::DataTooShort);
    }
    let data: &mut Config = from_bytes_mut(&mut data[..size_of::<Config>()]);
    if data.format != 0 {
        return Err(ReadAccountError::AlreadyInitialized);
    }
    data.format = FORMAT;
    data.authority = authority;
    Ok(data)
```

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L66-79)
```rust
pub fn validate_authority<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
    config: &AccountInfo<'a>,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let authority = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    ensure!(ProgramError::MissingRequiredSignature, authority.is_signer);
    ensure!(ProgramError::InvalidArgument, authority.is_writable);
    let config_data = config.data.borrow();
    let config = accounts::config::read(*config_data)?;
    ensure!(
        ProgramError::MissingRequiredSignature,
        authority.key.to_bytes() == config.authority
    );
    Ok(authority)
```

**File:** target_chains/solana/programs/pyth-price-store/src/instruction.rs (L10-26)
```rust
#[repr(u8)]
pub enum Instruction {
    // key[0] payer     [signer writable]
    // key[1] config    [writable]
    // key[2] system    []
    Initialize,
    // key[0] publisher        [signer writable]
    // key[1] publisher_config []
    // key[2] buffer           [writable]
    SubmitPrices,
    // key[0] autority         [signer writable]
    // key[1] config           []
    // key[2] publisher_config [writable]
    // key[3] buffer           [writable]
    // key[4] system           []
    InitializePublisher,
}
```
