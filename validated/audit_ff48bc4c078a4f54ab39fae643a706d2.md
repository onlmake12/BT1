### Title
OIS Delegation Rewards Permanently Lost When `undelegate` Is Called Without Prior `advance_delegation_record` — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The `undelegate` instruction in Pyth's Oracle Integrity Staking (OIS) `integrity_pool` on-chain program does not include the `delegation_record` account in its account list and therefore cannot enforce — or perform — a reward settlement before closing a delegation position. Any OIS rewards accrued since the last `advance_delegation_record` call are permanently lost when a staker undelegates.

---

### Finding Description

The OIS reward model works as follows:

1. Rewards accrue per epoch for each active delegation position.
2. `advance_delegation_record` settles those rewards: it reads `delegation_record.last_epoch`, iterates over pool events from that epoch to the current one, computes the staker's share, transfers tokens from `pool_reward_custody` to `stake_account_custody`, and updates `delegation_record.last_epoch` to the current epoch.
3. `undelegate` closes (or reduces) a delegation position by calling into the staking program's `close_position`.

The critical gap: the `undelegate` instruction's account list does **not** include `delegation_record`. [1](#0-0) 

Compare this to `advance_delegation_record`, which **does** include `delegation_record` (writable) as a required account: [2](#0-1) 

Because `delegation_record` is absent from `undelegate`'s accounts, the on-chain program:
- Cannot read `delegation_record.last_epoch` to detect stale accounting.
- Cannot call `advance_delegation_record` internally as a CPI.
- Cannot revert with `outdatedDelegatorAccounting` (error 6008) — that guard is unreachable from within `undelegate`. [3](#0-2) 

Once the position is removed from `stake_account_positions` by `undelegate`, the `advance_delegation_record` instruction has no position data to compute rewards against. The `delegation_record` PDA becomes an orphaned account with a stale `last_epoch`, and the accrued-but-unsettled rewards are permanently stranded in `pool_reward_custody`.

The official SDK's `unstakeFromPublisher` and `unstakeFromAllPublishers` methods confirm this: they submit only `undelegate` instructions with no preceding `advance_delegation_record` call. [4](#0-3) [5](#0-4) 

By contrast, `advanceDelegationRecord` is a completely separate, explicitly user-initiated call: [6](#0-5) 

---

### Impact Explanation

Any OIS staker who calls `undelegate` (directly on-chain or via the SDK) without first calling `advance_delegation_record` for every publisher they are delegated to will permanently lose all rewards accrued since their last settlement. The rewards remain locked in `pool_reward_custody` with no mechanism to recover them, because the position that entitled the staker to those rewards no longer exists. This is a **permanent freezing of unclaimed yield** for affected stakers.

---

### Likelihood Explanation

The likelihood is **high**:

1. The official SDK's `unstakeFromPublisher` and `unstakeFromAllPublishers` do not call `advance_delegation_record` before undelegating, meaning every user of the official staking UI who unstakes without manually claiming first loses their pending rewards.
2. Any user interacting with the on-chain program directly (e.g., via a custom script or CLI) faces the same risk with no on-chain guard to stop them.
3. Rewards expire one year from the epoch they were earned, creating time pressure that may cause users to rush undelegation without settling first. [7](#0-6) 

---

### Recommendation

The `undelegate` instruction should enforce that the `delegation_record` for the relevant publisher is up-to-date before allowing the position to be closed. Concretely:

1. Add `delegation_record` (keyed by `[publisher, stake_account_positions]`) as a required account in the `undelegate` instruction.
2. Inside the instruction handler, assert `delegation_record.last_epoch == current_epoch`, reverting with `OutdatedDelegatorAccounting` if not.
3. Alternatively, perform the reward settlement inline as part of `undelegate` (i.e., CPI into `advance_delegation_record` before closing the position).

Additionally, the SDK's `unstakeFromPublisher` and `unstakeFromAllPublishers` should prepend `advance_delegation_record` instructions for all affected publishers before the `undelegate` instructions. [4](#0-3) 

---

### Proof of Concept

1. Staker delegates to publisher P at epoch E.
2. Several epochs pass (E → E+N). Pool events are recorded; staker has accrued rewards.
3. Staker calls `undelegate` directly (or via `unstakeFromPublisher`) **without** calling `advance_delegation_record` first.
4. The position is removed from `stake_account_positions`. `delegation_record.last_epoch` remains at E (or wherever it was last advanced).
5. Staker attempts to call `advance_delegation_record` for publisher P. The instruction iterates over positions in `stake_account_positions` — but the position no longer exists. No rewards are computed or transferred.
6. The rewards for epochs E through E+N remain in `pool_reward_custody` and are permanently inaccessible to the staker. [8](#0-7) [9](#0-8)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L311-346)
```json
        {
          "name": "delegation_record",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  100, 101, 108, 101, 103, 97, 116, 105, 111, 110, 95, 114, 101,
                  99, 111, 114, 100
                ]
              },
              {
                "kind": "account",
                "path": "publisher"
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [],
      "discriminator": [155, 43, 226, 175, 227, 115, 33, 88],
      "name": "advance_delegation_record",
      "returns": "u64"
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L962-1080)
```json
      "accounts": [
        {
          "name": "owner",
          "signer": true,
          "writable": true
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "config_account",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
            },
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "stake_account_positions",
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "stake_account_metadata",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
            },
            "seeds": [
              {
                "kind": "const",
                "value": [
                  115, 116, 97, 107, 101, 95, 109, 101, 116, 97, 100, 97, 116,
                  97
                ]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "stake_account_custody",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
            },
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          }
        },
        {
          "address": "pytS9TjG1qyAZypk7n8rw8gfW9sUaqqYyMhJQ4E7JCQ",
          "name": "staking_program"
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [
        {
          "name": "position_index",
          "type": "u8"
        },
        {
          "name": "amount",
          "type": "u64"
        }
      ],
      "discriminator": [131, 148, 180, 198, 91, 104, 42, 238],
      "name": "undelegate"
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1195-1208)
```json
      "name": "DelegationRecord",
      "type": {
        "fields": [
          {
            "name": "last_epoch",
            "type": "u64"
          },
          {
            "name": "next_slash_event_index",
            "type": "u64"
          }
        ],
        "kind": "struct"
      }
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1365-1402)
```typescript
  errors: [
    {
      code: 6000;
      name: "publisherNotFound";
    },
    {
      code: 6001;
      name: "publisherOrRewardAuthorityNeedsToSign";
    },
    {
      code: 6002;
      name: "stakeAccountOwnerNeedsToSign";
    },
    {
      code: 6003;
      name: "outdatedPublisherAccounting";
    },
    {
      code: 6004;
      name: "tooManyPublishers";
    },
    {
      code: 6005;
      name: "unexpectedPositionState";
    },
    {
      code: 6006;
      name: "poolDataAlreadyUpToDate";
    },
    {
      code: 6007;
      name: "outdatedPublisherCaps";
    },
    {
      code: 6008;
      name: "outdatedDelegatorAccounting";
    },
    {
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

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L403-441)
```typescript
  public async unstakeFromAllPublishers(
    stakeAccountPositions: PublicKey,
    positionStates: (PositionState.LOCKED | PositionState.LOCKING)[],
  ) {
    const [stakeAccountPositionsData, currentEpoch] = await Promise.all([
      this.getStakeAccountPositions(stakeAccountPositions),
      getCurrentEpoch(this.connection),
    ]);

    const instructions = await Promise.all(
      stakeAccountPositionsData.data.positions
        .map((position, index) => {
          const publisher =
            position.targetWithParameters.integrityPool?.publisher;
          return publisher === undefined
            ? undefined
            : { index, position, publisher };
        })
        // By separating this filter from the next, typescript can narrow the
        // type and automatically infer that there will be no `undefined` values
        // in the array after this line.  If we combine those filters,
        // typescript won't narrow properly.
        .filter((positionInfo) => positionInfo !== undefined)
        .filter(({ position }) =>
          (positionStates as PositionState[]).includes(
            getPositionState(position, currentEpoch),
          ),
        )
        .reverse()
        .map(({ position, index, publisher }) =>
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(position.amount))
            .accounts({ publisher, stakeAccountPositions })
            .instruction(),
        ),
    );

    return sendTransaction(instructions, this.connection, this.wallet);
  }
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
