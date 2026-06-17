The code is clear. Let me verify the exact constraint and seed derivation before concluding.

### Title
Unprivileged Relayer Can Permanently Skip Intermediate Governance VAAs via Out-of-Order Sequence Execution — (`governance/remote_executor/programs/remote-executor/src/lib.rs`)

---

### Summary

The `execute_posted_vaa` instruction in `remote_executor` uses a **per-emitter** `ClaimRecord` with a strictly-increasing sequence check (`posted_vaa.sequence > claim_record.sequence`). Any unprivileged relayer can relay a legitimately guardian-signed VAA with a high sequence number first, advancing `claim_record.sequence` past all intermediate sequences and permanently blocking those governance VAAs from ever executing.

---

### Finding Description

The replay guard in `ExecutePostedVaa` is:

```
constraint = posted_vaa.sequence > claim_record.sequence
``` [1](#0-0) 

On success, the record is updated to the executed VAA's sequence:

```rust
claim_record.sequence = posted_vaa.sequence;
``` [2](#0-1) 

The `ClaimRecord` PDA is seeded **only by emitter address** — one record per emitter, not one per VAA:

```rust
seeds = [CLAIM_RECORD_SEED.as_bytes(), &posted_vaa.emitter_address]
``` [3](#0-2) 

`ClaimRecord` defaults to `sequence = 0`: [4](#0-3) 

The `payer` is an arbitrary signer — no privileged role is required to call `execute_posted_vaa`: [5](#0-4) 

**Attack path:**

1. Governance emitter issues VAAs with sequences 1, 2, …, 1000, all signed by Wormhole guardians and available on the Wormhole network.
2. Attacker (any relayer) posts VAA sequence=1000 to the Wormhole bridge on Solana and calls `execute_posted_vaa` with it. The check `1000 > 0` passes.
3. `claim_record.sequence` is now 1000.
4. Any subsequent attempt to execute VAAs 1–999 fails with `NonIncreasingSequence` because `N < 1000` for all N in 1–999. They are permanently unexecutable.

This contrasts with the per-VAA `Claim` PDA model used elsewhere in the codebase, which seeds the PDA by `(emitter, chain, sequence)` and thus allows out-of-order execution without blocking: [6](#0-5) 

---

### Impact Explanation

Governance VAAs for Pyth include data source updates, fee changes, and other oracle configuration actions. Permanently skipping intermediate governance VAAs means:

- Data source allow-lists may not be updated, causing the oracle to continue accepting prices from sources that should have been removed (or rejecting sources that should have been added).
- Fee or threshold parameters remain at stale values.
- The net effect is that price acceptance logic operates on an outdated governance state, enabling stale or manipulated prices to pass validation.

---

### Likelihood Explanation

The attacker needs only:
1. Access to a legitimately guardian-signed governance VAA with a high sequence number (publicly observable on the Wormhole network).
2. Enough SOL to pay transaction fees.
3. No privileged role, no key compromise, no Sybil attack.

Any permissionless relayer can execute this. The Wormhole protocol itself imposes no ordering requirement on VAA relay — ordering enforcement is entirely the responsibility of the consuming application, which `remote_executor` fails to implement correctly.

---

### Recommendation

Replace the per-emitter monotonic sequence model with a **per-VAA claim PDA** seeded by `(emitter, chain, sequence)`, matching the pattern already used in `wormhole-solana`: [7](#0-6) 

This marks each VAA as individually claimed (boolean flag) rather than tracking a high-water mark, which:
- Prevents replay of any individual VAA.
- Allows legitimate out-of-order relay without permanently blocking any VAA.
- Removes the sequence-skipping attack surface entirely.

---

### Proof of Concept

State-transition test (pseudocode matching the existing simulator pattern):

```rust
// Setup: emitter issues VAAs at sequences 1 and 100
let vaa_seq_1   = bench.add_vaa_account_with_seq(&emitter, seq=1,   &[governance_ix_1]);
let vaa_seq_100 = bench.add_vaa_account_with_seq(&emitter, seq=100, &[governance_ix_100]);

let mut sim = bench.start().await;

// Attacker relays sequence=100 first
sim.execute_posted_vaa(&vaa_seq_100, &vec![], ExecutorAttack::None)
    .await.unwrap();

// claim_record.sequence is now 100
let record = sim.get_claim_record(emitter).await;
assert_eq!(record.sequence, 100);

// Legitimate VAA at sequence=1 is now permanently blocked
let err = sim.execute_posted_vaa(&vaa_seq_1, &vec![], ExecutorAttack::None)
    .await.unwrap_err().unwrap();
assert_eq!(err, ExecutorError::NonIncreasingSequence.into());
// governance_ix_1 (e.g., data source update) can never execute
``` [8](#0-7)

### Citations

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L32-32)
```rust
        claim_record.sequence = posted_vaa.sequence;
```

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L63-64)
```rust
    #[account(mut)]
    pub payer: Signer<'info>,
```

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L65-65)
```rust
    #[account(constraint = Chain::from(posted_vaa.emitter_chain) == Solana @ ExecutorError::EmitterChainNotSolana, constraint = posted_vaa.sequence > claim_record.sequence @ExecutorError::NonIncreasingSequence, constraint = (&posted_vaa.magic == b"vaa" || &posted_vaa.magic == b"msg" || &posted_vaa.magic == b"msu") @ExecutorError::PostedVaaHeaderWrongMagicNumber )]
```

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L68-69)
```rust
    #[account(init_if_needed, space = 8 + ClaimRecord::LEN, payer=payer, seeds = [CLAIM_RECORD_SEED.as_bytes(), &posted_vaa.emitter_address], bump)]
    pub claim_record: Account<'info, ClaimRecord>,
```

**File:** governance/remote_executor/programs/remote-executor/src/state/claim_record.rs (L7-11)
```rust
#[derive(Default, BorshSchema)]
/// This struct records
pub struct ClaimRecord {
    pub sequence: u64,
}
```

**File:** target_chains/solana/crates/wormhole-solana/src/accounts/claim.rs (L15-35)
```rust
pub struct ClaimSeeds {
    pub emitter: Pubkey,
    pub chain: Chain,
    pub sequence: u64,
}

impl Account for Claim {
    type Seeds = ClaimSeeds;
    type Output = Pubkey;

    fn key(id: &Pubkey, seeds: Self::Seeds) -> Pubkey {
        Pubkey::find_program_address(
            &[
                seeds.emitter.as_ref(),
                &u16::from(seeds.chain).to_be_bytes(),
                &seeds.sequence.to_be_bytes(),
            ],
            id,
        )
        .0
    }
```

**File:** governance/remote_executor/programs/remote-executor/src/tests/test_adversarial.rs (L1-10)
```rust
use {
    super::executor_simulator::{ExecutorAttack, ExecutorBench, VaaAttack},
    crate::error::ExecutorError,
    anchor_lang::prelude::{ErrorCode, ProgramError, Pubkey, Rent},
    solana_sdk::{
        instruction::InstructionError, native_token::LAMPORTS_PER_SOL,
        system_instruction::transfer, transaction::TransactionError,
    },
};

```
