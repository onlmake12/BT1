### Title
Epoch-Boundary Race Condition in `unstakeFromGovernance` / `unstakeFromPublisher` Causes Unintended Cooldown Instead of Immediate Warmup Cancellation — (`File: governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

`unstakeFromGovernance` and `unstakeFromPublisher` read `currentEpoch` off-chain to classify positions as `LOCKING` (warmup) or `LOCKED` (active), then build `closePosition` / `undelegate` instructions based on that snapshot. If the Solana epoch advances between the off-chain read and the moment the transaction is included in a block, a position that was `LOCKING` at read time is now `LOCKED` at execution time. The on-chain program evaluates position state at execution time, so `closePosition` initiates a cooldown period instead of immediately returning the tokens — the opposite of what the user intended.

---

### Finding Description

`cancelWarmupGovernance` and `cancelWarmupIntegrityStaking` both call into `unstakeFromGovernance` / `unstakeFromPublisher` with `positionState = PositionState.LOCKING`.

Inside those functions the client:

1. Fetches the current epoch off-chain via `getCurrentEpoch(this.connection)`.
2. Filters positions whose `getPositionState(position, currentEpoch) === LOCKING` (i.e. `activationEpoch > currentEpoch`).
3. Builds `closePosition` / `undelegate` instructions referencing those position indices.
4. Submits the transaction.

`getPositionState` returns `LOCKING` when `currentEpoch < position.activationEpoch`.

If the epoch advances from `N` to `N+1` before the transaction lands, a position with `activationEpoch = N+1` transitions from `LOCKING` to `LOCKED`. The on-chain staking program re-evaluates position state at execution time using the live clock. For a `LOCKED` position, `closePosition` sets `unlockingStart` and places the tokens in the cooldown queue. For a `LOCKING` position it would have returned them immediately.

The same race exists in `unstakeFromPublisher` (called by `cancelWarmupIntegrityStaking`) via the `undelegate` instruction on the integrity-pool program. [1](#0-0) [2](#0-1) [3](#0-2) 

The callers that expose this to end-users: [4](#0-3) [5](#0-4) 

---

### Impact Explanation

A user who clicks "Cancel Warmup" near an epoch boundary may instead trigger an unstake-with-cooldown. Their tokens are locked for approximately two additional epochs (~2 weeks on Solana mainnet) rather than being returned immediately. The user loses liquidity for a period they did not consent to, with no way to reverse the cooldown once initiated.

---

### Likelihood Explanation

Solana epochs are approximately 2–3 days. The race window is the time between the client's RPC call to `getCurrentEpoch` and the transaction's inclusion in a block — typically seconds to minutes. Any user who submits a cancel-warmup transaction within that window at an epoch boundary is affected. No attacker action is required; the epoch advances automatically. Users who submit transactions during periods of network congestion (longer confirmation times) face a higher probability of hitting the boundary.

---

### Recommendation

Pass the epoch number read off-chain into the transaction as an argument and have the on-chain program assert it matches `Clock::get().epoch`. Alternatively, add a client-side guard that re-reads the epoch immediately before broadcasting and aborts if it has changed:

```typescript
const epochBefore = await getCurrentEpoch(this.connection);
// ... build instructions ...
const epochAfter = await getCurrentEpoch(this.connection);
if (epochBefore !== epochAfter) {
  throw new Error("Epoch advanced during transaction construction; please retry");
}
return sendTransaction(instructions, this.connection, this.wallet);
```

For the on-chain path, the `closePosition` instruction could accept an optional `expected_epoch` argument and revert with a clear error if the live epoch does not match, analogous to a slippage guard.

---

### Proof of Concept

```
Epoch N is active. activationEpoch of position P = N+1.

1. User calls cancelWarmupGovernance(amount).
2. Client reads currentEpoch = N.
3. getPositionState(P, N) → LOCKING  (N < N+1).
4. Client builds closePosition(index=P, amount, {voting:{}}).
5. Epoch advances to N+1 (automatic, no attacker needed).
6. Transaction lands. On-chain clock epoch = N+1.
7. On-chain: getPositionState(P, N+1) → LOCKED  (N+1 >= N+1).
8. closePosition sets unlockingStart = N+1 → tokens enter cooldown.

Expected: tokens returned immediately (cancel warmup).
Actual:   tokens locked in cooldown for ~2 more epochs.
``` [6](#0-5)

### Citations

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L292-343)
```typescript
  public async unstakeFromGovernance(
    stakeAccountPositions: PublicKey,
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
          position.targetWithParameters.voting !== undefined &&
          positionState === getPositionState(position, currentEpoch),
      );

    for (const { position, index } of eligiblePositions) {
      if (position.amount < remainingAmount) {
        instructionPromises.push(
          this.stakingProgram.methods
            .closePosition(index, convertBigIntToBN(position.amount), {
              voting: {},
            })
            .accounts({
              stakeAccountPositions,
            })
            .instruction(),
        );
        remainingAmount -= position.amount;
      } else {
        instructionPromises.push(
          this.stakingProgram.methods
            .closePosition(index, convertBigIntToBN(remainingAmount), {
              voting: {},
            })
            .accounts({
              stakeAccountPositions,
            })
            .instruction(),
        );
        break;
      }
    }

    const instructions = await Promise.all(instructionPromises);
    return sendTransaction(instructions, this.connection, this.wallet);
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L346-371)
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
```

**File:** governance/pyth_staking_sdk/src/utils/position.ts (L17-38)
```typescript
export const getPositionState = (
  position: Position,
  currentEpoch: bigint,
): PositionState => {
  if (currentEpoch < position.activationEpoch) {
    return PositionState.LOCKING;
  }
  if (!position.unlockingStart) {
    return PositionState.LOCKED;
  }
  const hasActivated = position.activationEpoch <= currentEpoch;
  const unlockStarted = position.unlockingStart <= currentEpoch;
  const unlockEnded = position.unlockingStart + 1n <= currentEpoch;

  if (hasActivated && !unlockStarted) {
    return PositionState.PREUNLOCKING;
  } else if (unlockStarted && !unlockEnded) {
    return PositionState.UNLOCKING;
  } else {
    return PositionState.UNLOCKED;
  }
};
```

**File:** apps/staking/src/api.ts (L338-348)
```typescript
export const cancelWarmupGovernance = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
  amount: bigint,
): Promise<void> => {
  await client.unstakeFromGovernance(
    stakeAccount,
    PositionState.LOCKING,
    amount,
  );
};
```

**File:** apps/staking/src/api.ts (L371-383)
```typescript
export const cancelWarmupIntegrityStaking = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
  publisherKey: PublicKey,
  amount: bigint,
): Promise<void> => {
  await client.unstakeFromPublisher(
    stakeAccount,
    publisherKey,
    PositionState.LOCKING,
    amount,
  );
};
```
