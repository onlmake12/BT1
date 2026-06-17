### Title
Unclaimable OIS Staking Rewards for Removed Publishers in `getAdvanceDelegationRecordInstructions` — (File: `governance/pyth_staking_sdk/src/pyth-staking-client.ts`)

---

### Summary

In Pyth's Oracle Integrity Staking (OIS) system, stakers permanently lose the ability to claim rewards earned while delegating to a publisher that is subsequently removed from `poolData.publishers`. The `getAdvanceDelegationRecordInstructions` function builds its instruction set exclusively from publishers returned by `extractPublisherData`, which filters out any slot set to `PublicKey.default`. Once a publisher is removed (zeroed out), no `advanceDelegationRecord` instruction is ever generated for them, and the on-chain program itself rejects any manual attempt to advance the record for a publisher not present in `pool_data`.

---

### Finding Description

`extractPublisherData` in `governance/pyth_staking_sdk/src/utils/pool.ts` constructs the active publisher list by filtering the fixed 1024-slot `poolData.publishers` array:

```typescript
return poolData.publishers
    .filter((publisher) => !publisher.equals(PublicKey.default))
    .map((publisher, index) => ({ ... }));
```

The existence of this filter is direct evidence that publisher slots can be set to `PublicKey.default` (i.e., publishers can be removed). [1](#0-0) 

`getAdvanceDelegationRecordInstructions` then uses this filtered list as the universe of publishers for which reward-claiming instructions are generated:

```typescript
const allPublishers = extractPublisherData(poolData);
const publishers = allPublishers
  .map((publisher) => {
    const positionsWithPublisher =
      stakeAccountPositionsData.data.positions.filter(
        ({ targetWithParameters }) =>
          targetWithParameters.integrityPool?.publisher.equals(publisher.pubkey),
      );
    ...
  })
  .filter(({ lowestEpoch }) => lowestEpoch !== undefined);
``` [2](#0-1) 

A staker's `stakeAccountPositions` still contains position entries referencing the removed publisher's pubkey. However, because that publisher is absent from `allPublishers`, the cross-reference loop never matches it, no `advanceDelegationRecord` instruction is produced for it, and the staker's accrued rewards for that publisher are never transferred. [3](#0-2) 

The on-chain `advance_delegation_record` instruction independently validates the `publisher` account against `pool_data` (IDL annotation: `"CHECK : The publisher will be checked against data in the pool_data"`), so even a manually crafted transaction bypassing the SDK would be rejected by the program for a removed publisher. [4](#0-3) 

The `PoolData` account stores publishers in a fixed array of 1024 `pubkey` slots alongside parallel arrays for `del_state`, `self_del_state`, `delegation_fees`, and `num_slash_events`, all indexed by position. [5](#0-4) 

---

### Impact Explanation

Any PYTH tokens earned as OIS delegation rewards during epochs when the staker was delegated to a publisher that is later removed become permanently unclaimable. The tokens remain locked in the pool reward custody account with no recovery path, because:

1. The SDK never generates the required `advanceDelegationRecord` instruction for the removed publisher.
2. The on-chain program rejects any direct call for a publisher not present in `pool_data`.

This is a direct loss of earned funds for affected stakers.

---

### Likelihood Explanation

Publisher removal is a governance/admin-level action (the `advance` instruction reconciles the publisher list against `publisher_caps`). However:

- Publishers legitimately leave the Pyth network (stop publishing symbols, get delisted).
- The `advance` instruction is permissionless and is called every epoch; it can update the publisher list based on the current `publisher_caps` account.
- Stakers may not claim rewards every epoch, creating a window where a publisher is removed before the staker calls `advanceDelegationRecord`.
- The `PoolData.publishers` array's filter-for-default design confirms removal is an expected operational event, not a hypothetical one.

---

### Recommendation

Before zeroing out a publisher slot in `poolData.publishers`, the protocol should ensure all `delegation_record` accounts for that publisher have been fully advanced to the current epoch. Alternatively, the `advance_delegation_record` on-chain instruction should be modified to accept a removed publisher (identified by a historical snapshot or a "tombstone" flag) so that stakers can still drain their pending rewards after removal. A simpler mitigation is to prohibit publisher removal until all delegators have claimed, analogous to the Suzaku fix.

---

### Proof of Concept

1. **Epoch N**: Staker delegates PYTH tokens to Publisher A. Publisher A earns rewards; `pool_data` records the reward ratio in `events[N]`.
2. **Epoch N+1**: Governance removes Publisher A — its slot in `poolData.publishers` is set to `PublicKey.default`.
3. **Epoch N+2**: Staker calls `advanceDelegationRecord` (via `client.advanceDelegationRecord(stakeAccountPositions)`).
4. `extractPublisherData(poolData)` returns a list that does **not** include Publisher A (filtered by `!publisher.equals(PublicKey.default)`).
5. `getAdvanceDelegationRecordInstructions` iterates only over the filtered list; Publisher A is never matched against the staker's positions.
6. No `advanceDelegationRecord` instruction is generated for Publisher A.
7. The staker's `delegation_record` for Publisher A is never advanced; rewards for epoch N are permanently locked in the pool reward custody.
8. A direct on-chain call with Publisher A's pubkey fails: the program's publisher-in-pool-data check rejects it.

### Citations

**File:** governance/pyth_staking_sdk/src/utils/pool.ts (L9-13)
```typescript
export const extractPublisherData = (
  poolData: PoolDataAccount,
): PublisherData => {
  return poolData.publishers
    .filter((publisher) => !publisher.equals(PublicKey.default))
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L692-713)
```typescript
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

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L284-290)
```json
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1381-1446)
```json
            "name": "publishers",
            "type": {
              "array": ["pubkey", 1024]
            }
          },
          {
            "name": "del_state",
            "type": {
              "array": [
                {
                  "defined": {
                    "name": "DelegationState"
                  }
                },
                1024
              ]
            }
          },
          {
            "name": "self_del_state",
            "type": {
              "array": [
                {
                  "defined": {
                    "name": "DelegationState"
                  }
                },
                1024
              ]
            }
          },
          {
            "name": "publisher_stake_accounts",
            "type": {
              "array": ["pubkey", 1024]
            }
          },
          {
            "name": "events",
            "type": {
              "array": [
                {
                  "defined": {
                    "name": "Event"
                  }
                },
                52
              ]
            }
          },
          {
            "name": "num_events",
            "type": "u64"
          },
          {
            "name": "num_slash_events",
            "type": {
              "array": ["u64", 1024]
            }
          },
          {
            "name": "delegation_fees",
            "type": {
              "array": ["u64", 1024]
            }
          }
```
