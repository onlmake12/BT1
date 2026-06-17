### Title
Delegator Positions Permanently Stuck When Publisher Is Removed From Integrity Pool — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`, `governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

When a publisher is removed from the OIS `pool_data` (their slot zeroed to `PublicKey.default`), any delegator who holds LOCKING (warmup) or LOCKED (staked) positions targeting that publisher loses the ability to call `undelegate` or `advance_delegation_record` for those positions. The positions cannot transition to cooldown and the underlying PYTH tokens become permanently inaccessible.

---

### Finding Description

The `undelegate` instruction in the integrity pool program explicitly validates that the supplied `publisher` account exists in `pool_data`:

> "CHECK : The publisher will be checked against data in the pool_data" [1](#0-0) 

If the publisher is absent, the program returns error `PublisherNotFound` (code 6000): [2](#0-1) 

The `advance_delegation_record` instruction carries the same constraint — it also requires the publisher to be present in `pool_data`. The SDK's `getAdvanceDelegationRecordInstructions` only generates instructions for publishers returned by `extractPublisherData`, which explicitly filters out zeroed-out (removed) publishers: [3](#0-2) 

Because `advance_delegation_record` must be called before `undelegate` (otherwise `OutdatedDelegatorAccounting` error 6008 is returned), a delegator with positions targeting a removed publisher faces a two-layer block:

1. `advance_delegation_record` cannot be constructed or executed for the removed publisher.
2. `undelegate` fails with `PublisherNotFound` even if attempted directly. [4](#0-3) 

The `unstakeFromPublisher` and `unstakeFromAllPublishers` SDK paths both route through `integrityPoolProgram.methods.undelegate`, so no alternative SDK path exists: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

**Impact: High.** Delegators' PYTH tokens in LOCKING (warmup) or LOCKED (staked) positions targeting the removed publisher are permanently frozen. They cannot be moved to cooldown, cannot be withdrawn, and cannot be redirected to another publisher. The staking custody account holds the tokens but no instruction path exists to release them.

---

### Likelihood Explanation

**Likelihood: Low.** Publisher removal is a privileged operation requiring `reward_program_authority` to sign. However, it is a normal, expected protocol operation (publisher voluntary exit, misbehavior, or epoch advancement removing a publisher no longer present in `publisher_caps`). The `advance` instruction — which is permissionless — updates pool state based on `publisher_caps`; if a publisher disappears from caps, they may be effectively removed during epoch advancement without any explicit privileged call. [7](#0-6) 

---

### Recommendation

When a publisher is removed from `pool_data`, the protocol should either:

1. **Force-close all active positions** targeting that publisher before removal, transitioning them directly to PREUNLOCKING (cooldown) state, analogous to the original report's recommendation of setting `redemptionCooldownPeriod` to 0 on `activeToSunset()`; or
2. **Allow `undelegate` to succeed** even when the publisher is absent from `pool_data`, so delegators can still exit their positions after removal.

---

### Proof of Concept

1. Delegator calls `stakeToPublisher` for publisher P → position created in LOCKING state.
2. Epoch advances → position transitions to LOCKED state.
3. `reward_program_authority` removes publisher P from `pool_data` (slot zeroed to `PublicKey.default`).
4. Delegator calls `unstakeFromPublisher(stakeAccount, P, LOCKED, amount)`:
   - SDK calls `integrityPoolProgram.methods.undelegate(index, amount).accounts({ publisher: P, stakeAccountPositions })`.
   - On-chain: publisher P not found in `pool_data.publishers` → error `PublisherNotFound` (6000).
5. Delegator calls `advanceDelegationRecord(stakeAccount)`:
   - `extractPublisherData` filters out P (equals `PublicKey.default`) → no instruction generated for P.
   - Delegation record for P remains outdated.
6. Any subsequent `undelegate` attempt also fails with `OutdatedDelegatorAccounting` (6008) even before reaching the `PublisherNotFound` check.
7. Delegator's PYTH tokens are permanently locked with no recovery path. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L39-73)
```json
      "code": 6000,
      "name": "PublisherNotFound"
    },
    {
      "code": 6001,
      "name": "PublisherOrRewardAuthorityNeedsToSign"
    },
    {
      "code": 6002,
      "name": "StakeAccountOwnerNeedsToSign"
    },
    {
      "code": 6003,
      "name": "OutdatedPublisherAccounting"
    },
    {
      "code": 6004,
      "name": "TooManyPublishers"
    },
    {
      "code": 6005,
      "name": "UnexpectedPositionState"
    },
    {
      "code": 6006,
      "name": "PoolDataAlreadyUpToDate"
    },
    {
      "code": 6007,
      "name": "OutdatedPublisherCaps"
    },
    {
      "code": 6008,
      "name": "OutdatedDelegatorAccounting"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L148-208)
```json
        {
          "name": "signer",
          "signer": true
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "publisher_caps"
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
          "name": "pool_reward_custody",
          "pda": {
            "program": {
              "kind": "const",
              "value": [
                140, 151, 37, 143, 78, 36, 137, 241, 187, 61, 16, 41, 20, 142,
                13, 131, 11, 90, 19, 153, 218, 255, 16, 132, 4, 142, 123, 216,
                219, 233, 248, 89
              ]
            },
            "seeds": [
              {
                "kind": "account",
                "path": "pool_config"
              },
              {
                "kind": "const",
                "value": [
                  6, 221, 246, 225, 215, 101, 161, 147, 217, 203, 225, 70, 206,
                  235, 121, 172, 28, 180, 133, 237, 95, 91, 55, 145, 58, 140,
                  245, 133, 126, 255, 0, 169
                ]
              },
              {
                "account": "PoolConfig",
                "kind": "account",
                "path": "pool_config.pyth_token_mint"
              }
            ]
          },
          "writable": true
        }
      ],
      "args": [],
      "discriminator": [7, 56, 108, 201, 36, 20, 57, 89],
      "name": "advance"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1125-1128)
```json
          "signer": true
        },
        {
          "name": "pool_config",
```

**File:** governance/pyth_staking_sdk/src/utils/pool.ts (L9-14)
```typescript
export const extractPublisherData = (
  poolData: PoolDataAccount,
): PublisherData => {
  return poolData.publishers
    .filter((publisher) => !publisher.equals(PublicKey.default))
    .map((publisher, index) => ({
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L376-382)
```typescript
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(position.amount))
            .accounts({
              publisher,
              stakeAccountPositions,
            })
            .instruction(),
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L432-436)
```typescript
        .map(({ position, index, publisher }) =>
          this.integrityPoolProgram.methods
            .undelegate(index, convertBigIntToBN(position.amount))
            .accounts({ publisher, stakeAccountPositions })
            .instruction(),
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
