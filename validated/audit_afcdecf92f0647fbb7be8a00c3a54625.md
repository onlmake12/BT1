### Title
Wrong Index Used After Filtering in `extractPublisherData` Causes Mismatched Parallel Array Lookups — (`File: governance/pyth_staking_sdk/src/utils/pool.ts`)

---

### Summary

`extractPublisherData` in the Pyth Staking SDK filters out default (empty) publisher slots from `poolData.publishers` before calling `.map()`. The `index` produced by `.map()` is the **filtered-array position**, not the **original position** in the 1024-slot `poolData.publishers` array. All parallel data arrays (`eventData`, `delegationFees`, `delState`, `selfDelState`, `publisherStakeAccounts`) are indexed by the **original** position. When gaps exist in the publishers array, every publisher that follows a gap is looked up at the wrong slot, producing incorrect delegation amounts, APY history, delegation fees, and — critically — a wrong `stakeAccount`. The wrong `stakeAccount` is then forwarded to `advanceDelegationRecord` instructions, causing reward-claim transactions to fail for affected delegators.

---

### Finding Description

`poolData.publishers` is a fixed-size array of 1024 `PublicKey` entries. Unused slots hold `PublicKey.default`. All parallel arrays (`delState`, `selfDelState`, `delegationFees`, `publisherStakeAccounts`, and `event.eventData` inside `events`) are co-indexed with `publishers` by the **original slot position**.

`extractPublisherData` first filters out default entries, then maps over the result:

```typescript
return poolData.publishers
  .filter((publisher) => !publisher.equals(PublicKey.default))
  .map((publisher, index) => ({          // ← index is filtered-array position
    apyHistory: poolData.events
      .filter((event) => event.epoch > 0n)
      .map((event) => ({
        apy: convertEpochYieldToApy(
          (event.y * (event.eventData[index]?.otherRewardRatio ?? 0n)) /  // ← WRONG index
            FRACTION_PRECISION_N,
        ) * computeDelegatorRewardPercentage(
          poolData.delegationFees[index] ?? 0n,                           // ← WRONG index
        ),
        ...
      })),
    delegationFee: poolData.delegationFees[index] ?? 0n,                  // ← WRONG index
    selfDelegation: poolData.selfDelState[index]?.totalDelegation ?? 0n,  // ← WRONG index
    stakeAccount: poolData.publisherStakeAccounts[index] === undefined ... // ← WRONG index
    totalDelegation: (poolData.delState[index]?.totalDelegation ?? 0n) +  // ← WRONG index
      (poolData.selfDelState[index]?.totalDelegation ?? 0n),
  }));
``` [1](#0-0) 

If `publishers[0]` is `PublicKey.default` and `publishers[1]` is `PublisherA`, the filtered array has `PublisherA` at filtered index `0`. The code then reads `delegationFees[0]`, `selfDelState[0]`, `publisherStakeAccounts[0]`, and `event.eventData[0]` — all of which belong to the empty slot, not to `PublisherA` whose data lives at original index `1`.

The corrupted `stakeAccount` value is then consumed directly in `getAdvanceDelegationRecordInstructions`:

```typescript
filteredPublishers.map(({ pubkey, stakeAccount }) =>
  this.integrityPoolProgram.methods
    .advanceDelegationRecord()
    .accountsPartial({
      publisherStakeAccountCustody: stakeAccount
        ? getStakeAccountCustodyAddress(stakeAccount)
        : null,
      publisherStakeAccountPositions: stakeAccount,   // ← wrong account passed on-chain
      ...
    })
    .instruction(),
),
``` [2](#0-1) 

The on-chain `advance_delegation_record` instruction validates that `publisherStakeAccountPositions` matches the publisher's registered stake account. Passing the wrong account causes the transaction to revert, blocking reward claims.

`getClaimableRewards` and `advanceDelegationRecord` (the public `claim` path) both call `getAdvanceDelegationRecordInstructions`, so both are affected. [3](#0-2) 

---

### Impact Explanation

- **Reward claim DoS**: Any staking user who has delegated to a publisher whose original slot index is greater than their filtered index (i.e., any publisher that follows a gap) will have `advanceDelegationRecord` built with the wrong `publisherStakeAccountPositions`. The on-chain program rejects the transaction, permanently blocking reward claims for those delegators until the SDK is patched.
- **Incorrect APY/delegation display**: `apyHistory`, `delegationFee`, `totalDelegation`, and `selfDelegation` are all read from the wrong slots, causing the staking UI to show incorrect data to all users.

---

### Likelihood Explanation

The trigger condition is a gap in `poolData.publishers` — a slot set to `PublicKey.default` with at least one real publisher at a higher index. This occurs if any publisher is ever removed or if the array is not strictly contiguous. The on-chain `PoolData` struct reserves 1024 publisher slots; the protocol is designed to support publisher removal/replacement. Once a single gap exists, every publisher after it is affected. The bug is silent (no error is thrown; wrong data is silently returned), making it hard to detect until reward claims start failing.

---

### Recommendation

Track the original index through the filter by using `reduce` or by mapping first and filtering after:

```typescript
return poolData.publishers
  .map((publisher, originalIndex) => ({ publisher, originalIndex }))
  .filter(({ publisher }) => !publisher.equals(PublicKey.default))
  .map(({ publisher, originalIndex }) => ({
    apyHistory: poolData.events
      .filter((event) => event.epoch > 0n)
      .map((event) => ({
        apy: convertEpochYieldToApy(
          (event.y * (event.eventData[originalIndex]?.otherRewardRatio ?? 0n)) /
            FRACTION_PRECISION_N,
        ) * computeDelegatorRewardPercentage(
          poolData.delegationFees[originalIndex] ?? 0n,
        ),
        epoch: event.epoch,
        selfApy: convertEpochYieldToApy(
          (event.y * (event.eventData[originalIndex]?.selfRewardRatio ?? 0n)) /
            FRACTION_PRECISION_N,
        ),
      }))
      .sort((a, b) => Number(a.epoch) - Number(b.epoch)),
    delegationFee: poolData.delegationFees[originalIndex] ?? 0n,
    pubkey: publisher,
    selfDelegation: poolData.selfDelState[originalIndex]?.totalDelegation ?? 0n,
    selfDelegationDelta: poolData.selfDelState[originalIndex]?.deltaDelegation ?? 0n,
    stakeAccount: ...(poolData.publisherStakeAccounts[originalIndex]),
    totalDelegation:
      (poolData.delState[originalIndex]?.totalDelegation ?? 0n) +
      (poolData.selfDelState[originalIndex]?.totalDelegation ?? 0n),
    totalDelegationDelta:
      (poolData.delState[originalIndex]?.deltaDelegation ?? 0n) +
      (poolData.selfDelState[originalIndex]?.deltaDelegation ?? 0n),
  }));
``` [4](#0-3) 

---

### Proof of Concept

1. Assume `poolData.publishers[0] = PublicKey.default` (gap), `poolData.publishers[1] = PublisherA`.
2. `poolData.publisherStakeAccounts[1]` = `StakeAccountA` (PublisherA's real stake account).
3. `extractPublisherData` filters out index 0, so PublisherA maps to filtered index 0.
4. `stakeAccount` is read as `poolData.publisherStakeAccounts[0]` = `PublicKey.default` → `null`.
5. `getAdvanceDelegationRecordInstructions` builds the instruction with `publisherStakeAccountPositions: null`.
6. The on-chain program expects `StakeAccountA`; receiving `null` causes the transaction to fail.
7. Any delegator to PublisherA calls `claim` → `advanceDelegationRecord` → transaction reverts → rewards permanently locked. [5](#0-4) [6](#0-5)

### Citations

**File:** governance/pyth_staking_sdk/src/utils/pool.ts (L9-49)
```typescript
export const extractPublisherData = (
  poolData: PoolDataAccount,
): PublisherData => {
  return poolData.publishers
    .filter((publisher) => !publisher.equals(PublicKey.default))
    .map((publisher, index) => ({
      apyHistory: poolData.events
        .filter((event) => event.epoch > 0n)
        .map((event) => ({
          apy:
            convertEpochYieldToApy(
              (event.y * (event.eventData[index]?.otherRewardRatio ?? 0n)) /
                FRACTION_PRECISION_N,
            ) *
            computeDelegatorRewardPercentage(
              poolData.delegationFees[index] ?? 0n,
            ),
          epoch: event.epoch,
          selfApy: convertEpochYieldToApy(
            (event.y * (event.eventData[index]?.selfRewardRatio ?? 0n)) /
              FRACTION_PRECISION_N,
          ),
        }))
        .sort((a, b) => Number(a.epoch) - Number(b.epoch)),
      delegationFee: poolData.delegationFees[index] ?? 0n,
      pubkey: publisher,
      selfDelegation: poolData.selfDelState[index]?.totalDelegation ?? 0n,
      selfDelegationDelta: poolData.selfDelState[index]?.deltaDelegation ?? 0n,
      stakeAccount:
        poolData.publisherStakeAccounts[index] === undefined ||
        poolData.publisherStakeAccounts[index].equals(PublicKey.default)
          ? null // eslint-disable-line unicorn/no-null
          : poolData.publisherStakeAccounts[index],
      totalDelegation:
        (poolData.delState[index]?.totalDelegation ?? 0n) +
        (poolData.selfDelState[index]?.totalDelegation ?? 0n),
      totalDelegationDelta:
        (poolData.delState[index]?.deltaDelegation ?? 0n) +
        (poolData.selfDelState[index]?.deltaDelegation ?? 0n),
    }));
};
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

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L739-756)
```typescript
    const advanceDelegationRecordInstructions = await Promise.all(
      filteredPublishers.map(({ pubkey, stakeAccount }) =>
        this.integrityPoolProgram.methods
          .advanceDelegationRecord()
          .accountsPartial({
            payer: payer ?? this.wallet.publicKey,
            publisher: pubkey,
            publisherStakeAccountCustody: stakeAccount
              ? getStakeAccountCustodyAddress(stakeAccount)
              : null, // eslint-disable-line unicorn/no-null
            publisherStakeAccountPositions: stakeAccount,
            stakeAccountCustody: getStakeAccountCustodyAddress(
              stakeAccountPositions,
            ),
            stakeAccountPositions,
          })
          .instruction(),
      ),
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L794-825)
```typescript
  public async getClaimableRewards(
    stakeAccountPositions: PublicKey,
    simulationPayer?: PublicKey,
  ) {
    const instructions = await this.getAdvanceDelegationRecordInstructions(
      stakeAccountPositions,
      simulationPayer,
    );

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

    return {
      expiry:
        instructions.lowestEpoch === undefined
          ? undefined
          : epochToDate(instructions.lowestEpoch + 53n),
      totalRewards,
    };
  }
```
