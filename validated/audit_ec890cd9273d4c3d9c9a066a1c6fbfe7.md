The code is clear. Let me verify the exact constraint and seed derivation before concluding.

### Title
Unprivileged Relayer Can Permanently Skip Intermediate Governance VAAs via Out-of-Order Sequence Execution — (`governance/remote_executor/programs/remote-executor/src/lib.rs`)

---

### Summary

The `remote_executor` program uses a per-emitter `ClaimRecord` with a single monotonic `sequence` field and a strictly-greater-than check (`posted_vaa.sequence > claim_record.sequence`). Any unprivileged relayer can execute a high-sequence governance VAA before lower-sequence ones, permanently blocking all intermediate governance actions.

---

### Finding Description

The `execute_posted_vaa` instruction enforces replay protection via a single `ClaimRecord` PDA seeded only by the emitter address:

```
seeds = [CLAIM_RECORD_SEED.as_bytes(), &posted_vaa.emitter_address]
``` [1](#0-0) 

The only ordering constraint is:

```rust
constraint = posted_vaa.sequence > claim_record.sequence
``` [2](#0-1) 

After execution, the record is updated to the executed sequence:

```rust
claim_record.sequence = posted_vaa.sequence;
``` [3](#0-2) 

`ClaimRecord` stores only a single `u64`: [4](#0-3) 

This means: once sequence `N+K` is executed, all VAAs with sequences `1` through `N+K-1` are permanently unexecutable, because their sequence is no longer `> claim_record.sequence`. There is no per-VAA claim PDA (unlike the `wormhole-solana` `Claim` account which seeds by emitter + chain + **sequence**): [5](#0-4) 

The `remote_executor` design intentionally diverged from the per-VAA model, but the `>` check instead of `==` creates the skip window.

---

### Impact Explanation

Governance VAAs control critical Pyth parameters: data source updates, fee changes, emitter whitelisting, etc. If an attacker permanently skips governance VAAs (e.g., a data source update from sequence 2 to 99), the program continues operating under stale or attacker-favorable governance state. This directly satisfies the scoped impact: governance instructions affecting oracle price acceptance (data source updates) are permanently lost, causing stale or manipulated prices to be accepted.

---

### Likelihood Explanation

The attack requires only that:
1. Multiple governance VAAs exist simultaneously in the Wormhole guardian network (e.g., during any batch governance operation, or simply when a VAA is emitted but not yet relayed).
2. The attacker relays a higher-sequence VAA first — a fully permissionless operation, since `payer` is just a `Signer` with no privilege check. [6](#0-5) 

No key compromise, guardian collusion, or privileged access is required. The adversarial test suite does not test out-of-order sequence submission, confirming this vector is unguarded. [7](#0-6) 

---

### Recommendation

Replace the per-emitter monotonic sequence model with a per-VAA claim PDA (seeded by emitter + chain + sequence, as done in `wormhole-solana`'s `Claim` account), or change the constraint from `>` to `==` (strict in-order enforcement). The per-VAA PDA approach is preferred as it also prevents replay of any individual VAA without imposing ordering.

---

### Proof of Concept

State-transition test (pseudocode):

```rust
// Governance emits VAAs: seq=1, seq=2, seq=100
let vaa_seq1   = add_vaa_account(&emitter, &[instr_update_data_source], seq=1);
let vaa_seq2   = add_vaa_account(&emitter, &[instr_change_fee],         seq=2);
let vaa_seq100 = add_vaa_account(&emitter, &[instr_noop],               seq=100);

// Attacker relays seq=100 first (permissionless)
sim.execute_posted_vaa(&vaa_seq100, ...).await.unwrap();
// claim_record.sequence == 100

// Now seq=1 and seq=2 are permanently blocked:
// 1 > 100 == false  → NonIncreasingSequence error
// 2 > 100 == false  → NonIncreasingSequence error
assert!(sim.execute_posted_vaa(&vaa_seq1, ...).await.is_err());
assert!(sim.execute_posted_vaa(&vaa_seq2, ...).await.is_err());
// Data source update and fee change are permanently lost.
```

### Citations

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L32-32)
```rust
        claim_record.sequence = posted_vaa.sequence;
```

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L63-66)
```rust
    #[account(mut)]
    pub payer: Signer<'info>,
    #[account(constraint = Chain::from(posted_vaa.emitter_chain) == Solana @ ExecutorError::EmitterChainNotSolana, constraint = posted_vaa.sequence > claim_record.sequence @ExecutorError::NonIncreasingSequence, constraint = (&posted_vaa.magic == b"vaa" || &posted_vaa.magic == b"msg" || &posted_vaa.magic == b"msu") @ExecutorError::PostedVaaHeaderWrongMagicNumber )]
    pub posted_vaa: Account<'info, AnchorVaa>,
```

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L68-69)
```rust
    #[account(init_if_needed, space = 8 + ClaimRecord::LEN, payer=payer, seeds = [CLAIM_RECORD_SEED.as_bytes(), &posted_vaa.emitter_address], bump)]
    pub claim_record: Account<'info, ClaimRecord>,
```

**File:** governance/remote_executor/programs/remote-executor/src/state/claim_record.rs (L9-11)
```rust
pub struct ClaimRecord {
    pub sequence: u64,
}
```

**File:** target_chains/solana/crates/wormhole-solana/src/accounts/claim.rs (L25-35)
```rust
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

**File:** governance/remote_executor/programs/remote-executor/src/tests/test_adversarial.rs (L11-19)
```rust
#[tokio::test]
/// This test file tests that the executor fails (and checks the errors to make sure it fails for the right reason) when :
/// - The VAA has a bad format
/// - The VAA is not owned by the bridge
/// - The VAA was not emitted by Solana
/// - Another account is passed in place of the system program
/// - A claim_record account not seeded by the emitter of the VAA is passed

async fn test_adversarial() {
```
