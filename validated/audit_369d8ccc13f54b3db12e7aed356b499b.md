### Title
Missing Epoch Deadline in `close_position` / `undelegate` Staking Instructions Allows Tokens to Be Locked in Cooldown for an Extra Epoch — (`governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

The Pyth staking program's `close_position` (governance unstake) and `undelegate` (OIS unstake) instructions compute `unlocking_start` from `current_epoch` at execution time, with no caller-supplied maximum-epoch deadline. If a user's transaction is submitted near the end of a 7-day epoch and is delayed past the epoch boundary, `unlocking_start` is set one epoch later than the user intended, locking their tokens in cooldown for an extra 7 days beyond their expectation and without their control.

---

### Finding Description

The Pyth staking system operates on 7-day epochs. When a user unstakes (calls `close_position` for governance or `undelegate` for OIS), the on-chain program sets `unlocking_start = current_epoch + 1`. The position then transitions through:

- **PREUNLOCKING** while `current_epoch < unlocking_start`
- **UNLOCKING** while `current_epoch == unlocking_start`
- **UNLOCKED** once `current_epoch >= unlocking_start + 1`

This is confirmed by `getPositionState`:

```
const unlockStarted = position.unlockingStart <= currentEpoch;
const unlockEnded   = position.unlockingStart + 1n <= currentEpoch;
```

The UI also confirms the timing: cooldown for a PREUNLOCKING position ends at `epochToDate(currentEpoch + 2n)`, meaning tokens are unavailable for two full epochs from the moment `close_position` executes.

Neither `close_position` nor `undelegate` accepts a `max_epoch` (deadline) parameter. The `unlocking_start` epoch is determined entirely by when the transaction lands on-chain.

**Scenario:**
- Epoch N ends Thursday 00:00 UTC.
- User submits `close_position` on Wednesday at 23:55 UTC, expecting `unlocking_start = N+1` and tokens available at epoch `N+2`.
- Transaction is delayed (Solana congestion, validator skip, fee spike) and lands after Thursday 00:00 UTC.
- Now `current_epoch = N+1`, so `unlocking_start = N+2` and tokens are not available until epoch `N+3` — one full extra week.

The user cannot cancel or reverse the cooldown once `unlocking_start` is set; the only recourse is to wait.

The same issue applies to `create_position` / `delegate` (staking), where `activation_epoch = current_epoch + 1` is delayed by one epoch, causing tokens to remain in warmup (LOCKING) for an extra epoch. However, warmup can be cancelled, making the unstake path the more severe case.

---

### Impact Explanation

A staking user's tokens are locked in cooldown for up to 7 additional days beyond their intention, without any mechanism to cancel or recover. This is a temporary freeze of funds. A user who planned to have tokens available at a specific epoch (e.g., to repay a loan, participate in a governance vote, or respond to a slashing event) may be unable to do so.

---

### Likelihood Explanation

Solana's epoch boundary is a fixed, publicly known weekly event (every Thursday 00:00 UTC). Any transaction submitted in the final minutes of an epoch is at risk. Solana does experience periodic congestion and leader-skip events. The risk is not targeted — it is a constant probabilistic exposure for any user who unstakes near an epoch boundary. Given the number of stakers on the platform, this will materialize over time.

---

### Recommendation

Add a `max_epoch: u64` parameter to `close_position` and `undelegate`. At the start of instruction execution, assert:

```rust
require!(current_epoch <= max_epoch, ErrTransactionTooLate);
```

This allows users to express their intent and have the transaction fail safely (rather than silently locking funds for an extra epoch) if it lands after the intended epoch boundary.

---

### Proof of Concept

1. Observe that `EPOCH_DURATION = ONE_WEEK_IN_SECONDS` (7 days). [1](#0-0) 

2. `getCurrentEpoch` computes `timestamp / EPOCH_DURATION` — a discrete jump at each epoch boundary. [2](#0-1) 

3. `getPositionState` shows that `unlocking_start` determines the entire cooldown window; a one-epoch shift in `unlocking_start` shifts the unlock date by exactly one week. [3](#0-2) 

4. The UI confirms cooldown for a PREUNLOCKING position ends at `epochToDate(currentEpoch + 2n)`, i.e., two epochs after the current one — meaning `unlocking_start` was set to `current_epoch + 1` at execution time. [4](#0-3) 

5. `unstakeFromGovernance` calls `close_position` with no deadline argument; `unstakeFromPublisher` calls `undelegate` with no deadline argument. Both resolve `current_epoch` at call time from the chain, not from any user-supplied bound. [5](#0-4) [6](#0-5) 

6. The `close_position` instruction in the staking IDL accepts only `index`, `amount`, and `target_with_parameters` — no `max_epoch` field. [7](#0-6)

### Citations

**File:** governance/pyth_staking_sdk/src/constants.ts (L7-10)
```typescript
const ONE_WEEK_IN_SECONDS = 7n * ONE_DAY_IN_SECONDS;
export const ONE_YEAR_IN_SECONDS = 365n * ONE_DAY_IN_SECONDS;

export const EPOCH_DURATION = ONE_WEEK_IN_SECONDS;
```

**File:** governance/pyth_staking_sdk/src/utils/clock.ts (L14-18)
```typescript
export const getCurrentEpoch: (connection: Connection) => Promise<bigint> =
  async (connection: Connection) => {
    const timestamp = await getCurrentSolanaTimestamp(connection);
    return timestamp / EPOCH_DURATION;
  };
```

**File:** governance/pyth_staking_sdk/src/utils/position.ts (L17-37)
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
```

**File:** apps/staking/src/components/ProgramSection/index.tsx (L214-218)
```typescript
          {cooldown > 0n && (
            <div className="mt-2 text-xs text-neutral-500">
              <Tokens>{cooldown}</Tokens> end{" "}
              <Date options="time">{epochToDate(currentEpoch + 2n)}</Date>
            </div>
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L292-344)
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
  }
```

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

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L638-658)
```json
      "args": [
        {
          "name": "target_with_parameters",
          "type": {
            "defined": {
              "name": "TargetWithParameters"
            }
          }
        },
        {
          "name": "amount",
          "type": "u64"
        }
      ],
      "discriminator": [48, 215, 197, 153, 96, 203, 180, 133],
      "docs": [
        "Creates a position",
        "Looks for the first available place in the array, fails if array is full",
        "Computes risk and fails if new positions exceed risk limit"
      ],
      "name": "create_position"
```
