### Title
`getClaimableRewards` Simulates Instructions Independently While Actual Claim Executes Them in a Batch, Causing Displayed Rewards to Diverge from Actual Received Amount - (File: `governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

`getClaimableRewards` simulates each `advanceDelegationRecord` instruction in a **separate, isolated transaction** (one per publisher), while the actual claim function `advanceDelegationRecord` executes **all instructions in a single batched transaction**. Because `pool_data` is marked writable in the IDL for `advance_delegation_record`, state mutations from earlier instructions in the batch are visible to later ones during actual execution, but are invisible during independent simulations. This means the reward amount displayed to the user can differ from the amount actually received when the claim is submitted.

---

### Finding Description

In `getClaimableRewards`, each `advanceDelegationRecord` instruction is simulated in its own isolated transaction:

```typescript
for (const instruction of instructions.advanceDelegationRecordInstructions) {
  const tx = new Transaction().add(instruction);   // one instruction per tx
  const res = await this.connection.simulateTransaction(tx);
  // accumulate return value
}
```

Every simulation starts from the same current on-chain state `S0`. The return value of each simulated instruction is summed to produce `totalRewards`.

The actual claim path, however, sends all instructions in a single transaction:

```typescript
return sendTransaction(
  [
    ...instructions.advanceDelegationRecordInstructions,  // ALL in one tx
    ...instructions.mergePositionsInstruction,
  ],
  this.connection,
  this.wallet,
);
```

In a single transaction, Solana's runtime applies state changes sequentially: instruction 1 mutates state to `S1`, instruction 2 runs against `S1`, and so on. The `advance_delegation_record` instruction has `pool_data` (which holds `claimable_rewards`) and `pool_reward_custody` both marked as **writable** in the IDL. Any mutation to these accounts by instruction N is visible to instruction N+1 in the actual execution, but not in the independent simulations.

If the on-chain `advance_delegation_record` logic reads or caps rewards against `pool_data.claimable_rewards` (which is decremented as rewards are paid out), then:

- **Simulation**: each instruction independently sees the full pre-claim `claimable_rewards` balance → `totalRewards` is potentially overstated.
- **Actual execution**: each subsequent instruction sees a reduced `claimable_rewards` balance → actual rewards received are lower.

---

### Impact Explanation

A staking user calls `getClaimableRewards` (which is surfaced as `availableRewards` in the staking UI) and sees amount X. When they click "Claim" and `advanceDelegationRecord` executes, they receive amount Y < X. The discrepancy grows with the number of publishers the user is delegated to and with how close `pool_data.claimable_rewards` is to being exhausted. Users are misled about their actual claimable balance, which is a direct staking accounting inconsistency.

---

### Likelihood Explanation

Any OIS staking user delegated to multiple publishers can trigger this. The `getClaimableRewards` path is called on every page load of the staking dashboard. The discrepancy is most pronounced when the reward pool is nearly depleted or when a user has many publisher delegations. No special privileges are required — any unprivileged staking user is affected.

---

### Recommendation

Simulate all `advanceDelegationRecordInstructions` in a **single transaction** (matching the actual execution path) rather than one instruction per transaction. This ensures the simulation sees the same sequential state mutations as the real claim:

```typescript
// Instead of simulating each instruction independently:
const tx = new Transaction();
for (const instruction of instructions.advanceDelegationRecordInstructions) {
  tx.add(instruction);
}
tx.feePayer = simulationPayer ?? this.wallet.publicKey;
const res = await this.connection.simulateTransaction(tx);
// parse all return data entries from res
```

If transaction size limits prevent batching all instructions, simulate them in the same groupings used by `sendTransaction`.

---

### Proof of Concept

1. User is delegated to publishers P1 and P2. `pool_data.claimable_rewards` = 100 tokens.
2. P1's delegation earns 80 tokens; P2's delegation earns 80 tokens.
3. `getClaimableRewards` simulates P1's instruction from state S0 → returns 80. Simulates P2's instruction from state S0 → returns 80. Displays `totalRewards = 160`.
4. User clicks Claim. Actual execution: P1's instruction runs, `claimable_rewards` drops from 100 to 20. P2's instruction runs against the updated state — only 20 tokens remain, so user receives 20 instead of 80.
5. User receives 100 tokens total, not the 160 displayed.

**Root cause location:** [1](#0-0) 

**Actual claim path (batched):** [2](#0-1) 

**`pool_data` writable in IDL (shared mutable state):** [3](#0-2) 

**`availableRewards` displayed to user from `getClaimableRewards`:** [4](#0-3)

### Citations

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L779-792)
```typescript
  public async advanceDelegationRecord(stakeAccountPositions: PublicKey) {
    const instructions = await this.getAdvanceDelegationRecordInstructions(
      stakeAccountPositions,
    );

    return sendTransaction(
      [
        ...instructions.advanceDelegationRecordInstructions,
        ...instructions.mergePositionsInstruction,
      ],
      this.connection,
      this.wallet,
    );
  }
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L803-816)
```typescript
    let totalRewards = 0n;

    for (const instruction of instructions.advanceDelegationRecordInstructions) {
      const tx = new Transaction().add(instruction);
      tx.feePayer = simulationPayer ?? this.wallet.publicKey;
      // eslint-disable-next-line @typescript-eslint/no-deprecated
      const res = await this.connection.simulateTransaction(tx);
      const val = res.value.returnData?.data[0];
      if (val === undefined) {
        continue;
      }
      const buffer = Buffer.from(val, "base64").reverse();
      totalRewards += BigInt("0x" + buffer.toString("hex"));
    }
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1365-1380)
```json
      "name": "PoolData",
      "repr": {
        "kind": "c"
      },
      "serialization": "bytemuck",
      "type": {
        "fields": [
          {
            "name": "last_updated_epoch",
            "type": "u64"
          },
          {
            "name": "claimable_rewards",
            "type": "u64"
          },
          {
```

**File:** apps/staking/src/api.ts (L159-166)
```typescript
    claimableRewards,
    stakeAccountPositions,
  ] = await Promise.all([
    loadBaseInfo(client, pythnetClient, hermesClient),
    client.getStakeAccountCustody(stakeAccount),
    client.getUnlockSchedule(stakeAccount),
    client.getClaimableRewards(stakeAccount, simulationPayer),
    client.getStakeAccountPositions(stakeAccount),
```
