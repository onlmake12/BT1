### Title
Stale Governance VAA Executable Indefinitely Without Timestamp Expiration - (`governance/remote_executor/programs/remote-executor/src/lib.rs`)

---

### Summary

The Pyth remote executor's `execute_posted_vaa` instruction enforces only a monotonically-increasing sequence number check, with no timestamp-based expiration. A governance VAA that was legitimately created and signed by Wormhole guardians but never executed remains permanently executable by any unprivileged actor, as long as no newer VAA has been executed since. This is a direct analog to the OlympusGovernance "active proposal does not expire" class of vulnerability.

---

### Finding Description

The `execute_posted_vaa` instruction in `governance/remote_executor/programs/remote-executor/src/lib.rs` is the Solana-side execution path for Pyth cross-chain governance. It accepts any posted Wormhole VAA from the governance emitter and executes its embedded Solana instructions via CPI, signing as the executor PDA.

The only replay/ordering protection is a sequence number constraint:

```rust
constraint = posted_vaa.sequence > claim_record.sequence
    @ ExecutorError::NonIncreasingSequence
``` [1](#0-0) 

The `ClaimRecord` stores only the last executed sequence number:

```rust
pub struct ClaimRecord {
    pub sequence: u64,
}
``` [2](#0-1) 

There is no check on the VAA's timestamp, creation time, or any wall-clock expiration. The `execute_posted_vaa` handler does not inspect `posted_vaa.timestamp` at all:

```rust
pub fn execute_posted_vaa(ctx: Context<ExecutePostedVaa>) -> Result<()> {
    let posted_vaa = &ctx.accounts.posted_vaa;
    let claim_record = &mut ctx.accounts.claim_record;
    claim_record.sequence = posted_vaa.sequence;
    let payload = ExecutorPayload::try_from_slice(&posted_vaa.payload)?;
    payload.check_header()?;
    // ... executes arbitrary CPIs
``` [3](#0-2) 

The same pattern exists in the EVM executor:

```solidity
if (vm.sequence <= lastExecutedSequence)
    revert ExecutorErrors.MessageOutOfOrder();
lastExecutedSequence = vm.sequence;
``` [4](#0-3) 

And in the CosmWasm contract:

```rust
if vaa.sequence <= state.governance_sequence_number {
    Err(PythContractError::OldGovernanceMessage)?;
} else {
    updated_config.governance_sequence_number = vaa.sequence;
}
``` [5](#0-4) 

---

### Impact Explanation

A governance VAA (e.g., sequence N) that was created and signed by Wormhole guardians but never executed remains permanently executable by any unprivileged actor, provided no VAA with sequence > N has been executed since. The remote executor can invoke arbitrary Solana instructions signed by the executor PDA — including transfers, account creation, and CPI calls to any program. If a stale VAA encodes a parameter change (e.g., fee update, data source change, contract upgrade) that was appropriate at creation time but is now harmful given changed protocol conditions, any actor can execute it months or years later.

The `ClaimRecord` is seeded per-emitter, so executing a newer VAA (sequence > N) permanently blocks the stale one. However, if governance activity is low or a chain is temporarily inactive, the window for stale execution is unbounded. [6](#0-5) 

---

### Likelihood Explanation

The attack path is fully permissionless: Wormhole VAAs are public. Any actor can fetch an old, unexecuted governance VAA from the Wormhole guardian network, post it to Solana via Wormhole's `post_vaa` instruction (also permissionless), and then call `execute_posted_vaa`. The only precondition is that no newer VAA has been executed since the stale one was created — a realistic condition during governance inactivity, chain downtime, or when a governance action was intentionally deferred but never formally cancelled.

---

### Recommendation

Add a maximum VAA age check inside `execute_posted_vaa` by comparing the VAA's embedded timestamp against the current Solana clock:

```rust
let vaa_age = Clock::get()?.unix_timestamp
    .checked_sub(posted_vaa.timestamp as i64)
    .ok_or(ExecutorError::Overflow)?;
require!(vaa_age <= MAX_VAA_AGE_SECONDS, ExecutorError::VaaExpired);
```

A reasonable bound (e.g., 30 days) would prevent execution of governance actions that are no longer contextually valid, while still allowing sufficient time for multi-chain rollout. Apply the same fix to the EVM `Executor.sol` and CosmWasm `execute_governance_instruction` paths. [3](#0-2) 

---

### Proof of Concept

1. Governance creates VAA sequence #N encoding a `SetFee` or arbitrary CPI instruction, signed by Wormhole guardians.
2. The VAA is never executed (governance moves on, chain is down, or the action is informally abandoned).
3. Six months later, `claim_record.sequence` is still N-1 (no newer VAA executed).
4. Attacker fetches the raw VAA bytes from the Wormhole guardian API (public endpoint).
5. Attacker calls Wormhole's `post_vaa` on Solana — permissionless, no special role required.
6. Attacker calls `execute_posted_vaa` with the posted VAA account.
7. The sequence check passes (`N > N-1`). The stale governance instructions execute, potentially setting protocol parameters to values that are now harmful. [7](#0-6) [2](#0-1)

### Citations

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L29-55)
```rust
    pub fn execute_posted_vaa(ctx: Context<ExecutePostedVaa>) -> Result<()> {
        let posted_vaa = &ctx.accounts.posted_vaa;
        let claim_record = &mut ctx.accounts.claim_record;
        claim_record.sequence = posted_vaa.sequence;

        let payload = ExecutorPayload::try_from_slice(&posted_vaa.payload)?;
        payload.check_header()?;

        let (_, bump) = Pubkey::find_program_address(
            &[EXECUTOR_KEY_SEED.as_bytes(), &posted_vaa.emitter_address],
            &id(),
        );

        for instruction in payload.instructions.iter().map(Instruction::from) {
            // TO DO: We currently pass `remaining_accounts` down to the CPIs, is there a more efficient way to do it?
            invoke_signed(
                &instruction,
                ctx.remaining_accounts,
                &[&[
                    EXECUTOR_KEY_SEED.as_bytes(),
                    &posted_vaa.emitter_address,
                    &[bump],
                ]],
            )?;
        }
        Ok(())
    }
```

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L62-75)
```rust
pub struct ExecutePostedVaa<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(constraint = Chain::from(posted_vaa.emitter_chain) == Solana @ ExecutorError::EmitterChainNotSolana, constraint = posted_vaa.sequence > claim_record.sequence @ExecutorError::NonIncreasingSequence, constraint = (&posted_vaa.magic == b"vaa" || &posted_vaa.magic == b"msg" || &posted_vaa.magic == b"msu") @ExecutorError::PostedVaaHeaderWrongMagicNumber )]
    pub posted_vaa: Account<'info, AnchorVaa>,
    /// The reason claim_record has different seeds than executor_key is that executor key might need to pay in the CPI, so we want it to be a native wallet
    #[account(init_if_needed, space = 8 + ClaimRecord::LEN, payer=payer, seeds = [CLAIM_RECORD_SEED.as_bytes(), &posted_vaa.emitter_address], bump)]
    pub claim_record: Account<'info, ClaimRecord>,
    pub system_program: Program<'info, System>,
    // Additional accounts passed to the instruction will be passed down to the CPIs. Very importantly executor_key needs to be passed as it will be the signer of the CPIs.
    // Below is the "anchor specification" of that account
    // #[account(seeds = [EXECUTOR_KEY_SEED.as_bytes(), &posted_vaa.emitter_address], bump)]
    // pub executor_key: UncheckedAccount<'info>,
}
```

**File:** governance/remote_executor/programs/remote-executor/src/state/claim_record.rs (L9-11)
```rust
pub struct ClaimRecord {
    pub sequence: u64,
}
```

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L131-134)
```text
        if (vm.sequence <= lastExecutedSequence)
            revert ExecutorErrors.MessageOutOfOrder();

        lastExecutedSequence = vm.sequence;
```

**File:** target_chains/cosmwasm/contracts/pyth/src/contract.rs (L258-262)
```rust
    if vaa.sequence <= state.governance_sequence_number {
        Err(PythContractError::OldGovernanceMessage)?;
    } else {
        updated_config.governance_sequence_number = vaa.sequence;
    }
```
