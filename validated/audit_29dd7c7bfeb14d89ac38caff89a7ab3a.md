### Title
Permissionless `Initialize` Accepts Unreachable Authority, Permanently Bricking `InitializePublisher` — (`target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs`)

### Summary

The `Initialize` instruction has no access control and no validation on the `authority` argument. Any unprivileged attacker can front-run the legitimate deployer, call `Initialize` with `args.authority = [0u8; 32]`, and permanently lock the Config PDA to an authority that can never produce a valid signature. All subsequent `InitializePublisher` calls will fail forever.

---

### Finding Description

`initialize()` requires only that the **payer** is a signer — it imposes no restriction on who the payer is, and it performs zero validation on `args.authority` before writing it into the Config PDA. [1](#0-0) 

`validate_payer` only checks `is_signer` and `is_writable` — any funded account qualifies. [2](#0-1) 

`accounts::config::create` writes the caller-supplied `authority` bytes verbatim with no non-zero / validity check: [3](#0-2) 

The Config PDA is a one-time-write account — once `format` is set to `FORMAT`, any re-call returns `AlreadyInitialized`: [4](#0-3) 

`validate_authority` in `InitializePublisher` then requires the signer's key to exactly match `config.authority`: [5](#0-4) 

If `config.authority == [0u8; 32]` (the all-zeros pubkey / system program address), no Ed25519 private key exists that can produce a valid signature for it. The `is_signer` check at line 71 will always fail for any real account, and the key-equality check at line 77 will fail for any signable account.

---

### Impact Explanation

- The Config PDA is permanently frozen with an unreachable authority.
- `InitializePublisher` is permanently DoS'd — no publisher can ever be registered.
- `SubmitPrices` depends on a registered publisher config, so price submission is also blocked for all future publishers.
- The program cannot be re-initialized (one-time write guard).

---

### Likelihood Explanation

The attack requires only a funded Solana account and knowledge of the program ID (public information after deployment). The attacker monitors the chain for program deployment and submits `Initialize` before the legitimate operator. This is a standard front-running scenario on Solana, where transaction ordering is not guaranteed. No privileged access, leaked keys, or social engineering is required.

---

### Recommendation

1. **Validate `args.authority` is non-zero** inside `accounts::config::create` — reject `[0u8; 32]` explicitly.
2. **Restrict who can call `Initialize`** — require the payer to match a hard-coded upgrade authority or embed the expected authority in the program at compile time (e.g., via a `declare_id!`-style constant).
3. Alternatively, derive the authority from the payer's key directly (i.e., `args.authority = payer.key`) so the caller cannot supply an arbitrary value.

---

### Proof of Concept

```
1. Attacker submits Initialize(config_bump, authority=[0u8;32])
   - payer = attacker (valid signer, funded)
   - Config PDA does not yet exist → create succeeds
   - Config.authority is written as [0u8;32]

2. Legitimate operator submits Initialize(config_bump, authority=operator_key)
   - Config PDA already exists, format != 0
   - Returns AlreadyInitialized → fails

3. Anyone submits InitializePublisher(...)
   - validate_authority: authority.is_signer must be true AND
     authority.key.to_bytes() == [0u8;32]
   - No keypair exists for [0u8;32] → MissingRequiredSignature forever

Result: publisher registration is permanently bricked.
```

### Citations

**File:** target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs (L22-25)
```rust
    let mut accounts = accounts.iter();
    let payer = validate_payer(accounts.next())?;
    let config = validate_config(accounts.next(), args.config_bump, program_id, true)?;
    let system = validate_system(accounts.next())?;
```

**File:** target_chains/solana/programs/pyth-price-store/src/validate.rs (L32-39)
```rust
pub fn validate_payer<'a, 'b>(
    account: Option<&'b AccountInfo<'a>>,
) -> Result<&'b AccountInfo<'a>, ProgramError> {
    let payer = account.ok_or(ProgramError::NotEnoughAccountKeys)?;
    ensure!(ProgramError::MissingRequiredSignature, payer.is_signer);
    ensure!(ProgramError::InvalidArgument, payer.is_writable);
    Ok(payer)
}
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
