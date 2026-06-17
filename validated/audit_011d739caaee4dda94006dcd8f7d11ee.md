### Title
Unclaimed OIS Rewards Permanently Lost When User Fully Undelegates From a Publisher Without Prior Claim - (`governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

The `getAdvanceDelegationRecordInstructions` function in `pyth-staking-client.ts` only generates `advance_delegation_record` instructions for publishers where the user currently holds active positions. When a user fully undelegates all positions from a publisher via `unstakeFromPublisher` (or `unstakeFromAllPublishers`) without first calling `advanceDelegationRecord`, the SDK permanently loses the ability to generate the claim instruction for that publisher. Any accrued but unclaimed OIS rewards for epochs between `delegation_record.last_epoch` and the current epoch are silently abandoned.

---

### Finding Description

In `getAdvanceDelegationRecordInstructions`, the set of publishers eligible for reward advancement is built by filtering to only those publishers for which the user has at least one position with a defined `activationEpoch`:

```typescript
const publishers = allPublishers
  .map((publisher) => {
    const positionsWithPublisher =
      stakeAccountPositionsData.data.positions.filter(
        ({ targetWithParameters }) =>
          targetWithParameters.integrityPool?.publisher.equals(publisher.pubkey),
      );

    let lowestEpoch;
    for (const position of positionsWithPublisher) {
      lowestEpoch = bigintMin(position.activationEpoch, lowestEpoch);
    }

    return { ...publisher, lowestEpoch };
  })
  .filter(({ lowestEpoch }) => lowestEpoch !== undefined);   // ← drops publishers with no positions
``` [1](#0-0) 

If `lowestEpoch` is `undefined` (i.e., no positions remain for that publisher), the publisher is excluded from the list and no `advance_delegation_record` instruction is ever generated for it.

The `unstakeFromPublisher` function calls `undelegate` directly without first calling `advance_delegation_record`:

```typescript
instructionPromises.push(
  this.integrityPoolProgram.methods
    .undelegate(index, convertBigIntToBN(position.amount))
    .accounts({ publisher, stakeAccountPositions })
    .instruction(),
);
``` [2](#0-1) 

After all positions for a publisher are removed, any subsequent call to `advanceDelegationRecord` or `getClaimableRewards` will silently skip that publisher entirely:

```typescript
const filteredPublishers = publishers.filter((_, index) => {
  return !(delegationRecords[index]?.lastEpoch === currentEpoch);
});
``` [3](#0-2) 

The `DelegationRecord` on-chain account (`last_epoch`, `next_slash_event_index`) persists after undelegation, but the SDK never generates the instruction to advance it once positions are gone. [4](#0-3) 

---

### Impact Explanation

Any OIS staking user who fully undelegates from a publisher (via `unstakeFromPublisher` or `unstakeFromAllPublishers`) without first calling `claim` / `advanceDelegationRecord` permanently loses all accrued but unclaimed rewards for that publisher. The `DelegationRecord.last_epoch` is never advanced to the current epoch, so the reward tokens remain locked in the pool reward custody and are never transferred to the user's stake account custody. The loss is proportional to the number of epochs elapsed since the last claim and the user's delegated stake.

---

### Likelihood Explanation

This is a realistic scenario for any OIS delegator. The UI exposes an "Unstake" flow that calls `unstakeFromPublisher` without enforcing a prior claim step. A user who unstakes all their delegation to a publisher in a single action — which is the natural UX path — will silently lose rewards. No special knowledge or adversarial intent is required; the loss is triggered by normal user behavior.

---

### Recommendation

In `unstakeFromPublisher` and `unstakeFromAllPublishers`, call `advance_delegation_record` for the affected publisher(s) before executing the `undelegate` instructions. Alternatively, modify `getAdvanceDelegationRecordInstructions` to also include publishers for which a `DelegationRecord` exists on-chain but no active positions remain (i.e., check the on-chain `delegation_record.last_epoch < currentEpoch` independently of position existence). The `claim` step should be a mandatory prerequisite to full undelegation in the SDK.

---

### Proof of Concept

1. User delegates 1000 PYTH to publisher P at epoch 10. `delegation_record.last_epoch = 10`.
2. Epochs 11–15 pass. Rewards accrue for 5 epochs. User never calls `claim`.
3. At epoch 15, user calls `unstakeFromPublisher(stakeAccountPositions, P, LOCKED, 1000n)`.
   - This calls `undelegate` directly with no prior `advance_delegation_record`.
   - All positions for publisher P are removed from `stakeAccountPositions`.
4. User calls `claim(client, stakeAccountPositions)` → `advanceDelegationRecord(stakeAccountPositions)`.
   - `getAdvanceDelegationRecordInstructions` iterates `allPublishers`, maps positions, finds `positionsWithPublisher = []` for P, sets `lowestEpoch = undefined`.
   - Publisher P is filtered out at `.filter(({ lowestEpoch }) => lowestEpoch !== undefined)`.
   - No `advance_delegation_record` instruction is generated for P.
5. `delegation_record.last_epoch` for P remains at 10. Rewards for epochs 10–14 are never transferred. The user receives 0 rewards for those epochs. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L346-401)
```typescript
  public async unstakeFromPublisher(
    stakeAccountPositions: PublicKey,
    publisher: PublicKey,
    positionState: PositionState.LOCKED | PositionState.LOCKING,
    amount: bigint,
  ) {
    const stakeAccountPositionsData = await this.getStakeAccountPositions(
      stakeAccountPositions,
    );
    const currentEpoch = await getCurrentEpoch(this.connection);

    let remainingAmount = amount;
    const instructionPromises: Promise<TransactionInstruction>[] = [];

    const eligiblePositions = stakeAccountPositionsData.data.positions
      .map((p, i) => ({ index: i, position: p }))
      .reverse()
      .filter(
        ({ position }) =>
          position.targetWithParameters.integrityPool?.publisher !==
            undefined &&
          position.targetWithParameters.integrityPool.publisher.equals(
            publisher,
          ) &&
          positionState === getPositionState(position, currentEpoch),
      );

    for (const { position, index } of eligiblePositions) {
      if (position.amount < remainingAmount) {
        instructionPromises.push(
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(position.amount))
            .accounts({
              publisher,
              stakeAccountPositions,
            })
            .instruction(),
        );
        remainingAmount -= position.amount;
      } else {
        instructionPromises.push(
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(remainingAmount))
            .accounts({
              publisher,
              stakeAccountPositions,
            })
            .instruction(),
        );
        break;
      }
    }

    const instructions = await Promise.all(instructionPromises);
    return sendTransaction(instructions, this.connection, this.wallet);
  }
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L684-713)
```typescript
  async getAdvanceDelegationRecordInstructions(
    stakeAccountPositions: PublicKey,
    payer?: PublicKey,
  ) {
    const poolData = await this.getPoolDataAccount();
    const stakeAccountPositionsData = await this.getStakeAccountPositions(
      stakeAccountPositions,
    );
    const allPublishers = extractPublisherData(poolData);
    const publishers = allPublishers
      .map((publisher) => {
        const positionsWithPublisher =
          stakeAccountPositionsData.data.positions.filter(
            ({ targetWithParameters }) =>
              targetWithParameters.integrityPool?.publisher.equals(
                publisher.pubkey,
              ),
          );

        let lowestEpoch;
        for (const position of positionsWithPublisher) {
          lowestEpoch = bigintMin(position.activationEpoch, lowestEpoch);
        }

        return {
          ...publisher,
          lowestEpoch,
        };
      })
      .filter(({ lowestEpoch }) => lowestEpoch !== undefined);
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L733-735)
```typescript
    const filteredPublishers = publishers.filter((_, index) => {
      return !(delegationRecords[index]?.lastEpoch === currentEpoch);
    });
```

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

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1475-1488)
```typescript
      name: "delegationRecord";
      type: {
        kind: "struct";
        fields: [
          {
            name: "lastEpoch";
            type: "u64";
          },
          {
            name: "nextSlashEventIndex";
            type: "u64";
          },
        ];
      };
```
