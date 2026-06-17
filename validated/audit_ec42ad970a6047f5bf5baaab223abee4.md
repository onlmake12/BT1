The code confirms this is a real vulnerability. The existing test in `test_basic_instructions.rs` actually **demonstrates the exact attack** — it executes sequence 3 before sequence 2, and then proves sequence 2 is permanently unexecutable (line 127-137).

---

### Title
Sequence-Skipping Griefing via Monotonic-Only Replay Guard Allows Permanent Governance VAA Suppression — (`governance/remote_executor/programs/remote-executor/src/lib.rs`)

### Summary
The `execute_posted_vaa` instruction uses a single `ClaimRecord` per emitter with a monotonic-only sequence check (`posted_vaa.sequence > claim_record.sequence`). Any unprivileged caller can submit a higher-sequence governance VAA before lower-sequence ones are processed, permanently rendering all intermediate VAAs unexecutable.

### Finding Description

The `ExecutePostedVaa` account struct enforces only a strict-greater-than check: [1](#0-0) 

The `ClaimRecord` is a single PDA keyed only by emitter address — not by sequence: [2](#0-1) 

On execution, the record is unconditionally overwritten with the submitted VAA's sequence: [3](#0-2) 

There is no signer restriction on `payer` beyond it being a `Signer` (any funded wallet qualifies). There is no check that the submitted sequence is exactly `claim_record.sequence + 1`. Any valid Wormhole-posted VAA with a sequence number greater than the current record passes all constraints.

The `ClaimRecord` stores only a single `u64`: [4](#0-3) 

The existing test suite inadvertently proves the attack: `vaa_account_transfer2` (sequence 3) is submitted before `vaa_account_transfer1` (sequence 2), after which sequence 2 permanently fails with `NonIncreasingSequence`, and the account it would have created is confirmed `None`: [5](#0-4) 

### Impact Explanation

Governance VAAs carry instructions that update Pyth oracle data sources, emitter configurations, publisher permissions, and other critical on-chain state. An attacker who suppresses intermediate governance VAAs causes those configuration changes to be permanently skipped. The on-chain state remains at whatever configuration existed before the skipped VAAs, which may be stale or unauthorized. This directly matches the scoped impact: on-chain program flaws causing inaccurate or unauthorized oracle configurations to persist.

### Likelihood Explanation

The attack requires only:
1. A funded Solana wallet (unprivileged)
2. The ability to observe Wormhole-posted VAAs (they are public on-chain accounts)
3. Submitting a transaction to a public program instruction before the legitimate relayer does

The legitimate relayer (`crank_pythnet_relayer`) processes sequences strictly in order (incrementing by 1 each iteration). An attacker who front-runs it with any VAA at sequence N+K (K > 1) — which is already posted and guardian-signed on Wormhole — permanently blocks N+1 through N+K-1. This is trivially executable on any network where multiple governance VAAs are pending simultaneously.

### Recommendation

Replace the monotonic sequence check with a per-sequence claim PDA, keyed by `[CLAIM_RECORD_SEED, emitter_address, sequence_bytes]`. This is the pattern already used by the Wormhole core bridge's own governance handlers: [6](#0-5) 

Each VAA gets its own claim account that can only be initialized once, providing exact-once execution without ordering constraints. Alternatively, enforce strict sequential execution by requiring `posted_vaa.sequence == claim_record.sequence + 1`.

### Proof of Concept

The existing test already encodes the state transition. A targeted proof:

```
State: claim_record.sequence = 1 (seq 1 already executed)
VAAs available on Wormhole: seq 2 (update data source A), seq 3 (update data source B), seq 5 (unrelated)

Attack:
  tx1: execute_posted_vaa(vaa_seq_5)  → passes (5 > 1), claim_record.sequence = 5
  tx2: execute_posted_vaa(vaa_seq_2)  → fails NonIncreasingSequence (2 < 5)
  tx3: execute_posted_vaa(vaa_seq_3)  → fails NonIncreasingSequence (3 < 5)

Result: data source updates from seq 2 and seq 3 are permanently lost.
```

This matches the demonstrated behavior at: [7](#0-6)

### Citations

**File:** governance/remote_executor/programs/remote-executor/src/lib.rs (L31-32)
```rust
        let claim_record = &mut ctx.accounts.claim_record;
        claim_record.sequence = posted_vaa.sequence;
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

**File:** governance/remote_executor/programs/remote-executor/src/state/claim_record.rs (L9-11)
```rust
pub struct ClaimRecord {
    pub sequence: u64,
}
```

**File:** governance/remote_executor/programs/remote-executor/src/tests/test_basic_instructions.rs (L99-137)
```rust
    // Execute one transfer
    sim.execute_posted_vaa(&vaa_account_transfer2, &vec![], ExecutorAttack::None)
        .await
        .unwrap();

    // Check state post call
    let post_account4 = sim.get_account(receiver4.pubkey()).await.unwrap();
    assert_eq!(post_account4.lamports, Rent::default().minimum_balance(0));
    assert_eq!(post_account4.data.len(), 0);
    assert_eq!(post_account4.owner, system_program::id());

    let claim_record_data = sim.get_claim_record(emitter).await;
    assert_eq!(claim_record_data.sequence, 3);

    // Replay attack
    assert_eq!(
        sim.execute_posted_vaa(&vaa_account_transfer2, &vec![], ExecutorAttack::None)
            .await
            .unwrap_err()
            .unwrap(),
        ExecutorError::NonIncreasingSequence.into()
    );

    let claim_record_data = sim.get_claim_record(emitter).await;
    assert_eq!(claim_record_data.sequence, 3);

    // Using a governance message with a lower sequence number
    assert_eq!(
        sim.execute_posted_vaa(&vaa_account_transfer1, &vec![], ExecutorAttack::None)
            .await
            .unwrap_err()
            .unwrap(),
        ExecutorError::NonIncreasingSequence.into()
    );
    let claim_record_data = sim.get_claim_record(emitter).await;
    assert_eq!(claim_record_data.sequence, 3);

    let post_account3 = sim.get_account(receiver3.pubkey()).await;
    assert_eq!(post_account3, None);
```

**File:** target_chains/solana/programs/core-bridge/src/legacy/processor/governance/transfer_fees.rs (L36-47)
```rust
    vaa: UncheckedAccount<'info>,

    /// Claim account (mut), which acts as replay protection after consuming data from the VAA
    /// account.
    ///
    /// Seeds: [emitter_address, emitter_chain, sequence],
    /// seeds::program = core_bridge_program.
    ///
    /// CHECK: This account is created via [claim_vaa](crate::utils::vaa::claim_vaa).
    /// This account can only be created once for this VAA.
    #[account(mut)]
    claim: UncheckedAccount<'info>,
```
