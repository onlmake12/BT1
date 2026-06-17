### Title
`advance_delegation_record` Permissionless + No Zero-Reward Guard — Epoch Slot Consumed With Zero Yield, Locking Staker Out for One Epoch - (File: `governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The `advance_delegation_record` instruction in the OIS integrity pool program is permissionless (no owner/authority signer required — only a fee `payer`). It is epoch-rate-limited via `DelegationRecord.last_epoch`. An unprivileged attacker can call it for any staker's account at the start of an epoch, advancing `last_epoch` to the current epoch. If called when `y = 0` (currently the case per OP-PIP-103) or before `pool_data` has been updated for the current epoch, the staker's epoch slot is consumed with zero rewards, and the staker is locked out from claiming rewards for that epoch.

---

### Finding Description

The `advance_delegation_record` instruction is defined in the integrity pool program IDL with the following account structure:

- `payer` — signer, writable (fee payer only; **no authority or owner check**)
- `stakeAccountPositions` — the victim staker's account (no signer required)
- `poolData` — writable
- `poolConfig`
- `poolRewardCustody` — writable
- `publisher`
- `publisherStakeAccountPositions`
- `publisherStakeAccountCustody` — writable
- `stakeAccountCustody` — writable
- `delegationRecord` — writable (contains `last_epoch`)
- `tokenProgram`, `systemProgram` [1](#0-0) 

The `DelegationRecord` account type contains a single `last_epoch: u64` field, which acts as the epoch-based rate-limit gate: [2](#0-1) 

The TypeScript SDK client-side code explicitly filters out publishers where `lastEpoch === currentEpoch` before constructing `advance_delegation_record` instructions — this guard exists **only off-chain**: [3](#0-2) 

The `PoolData` account contains `last_updated_epoch` and an `events` array (capped at 52 entries) that stores per-epoch reward data: [4](#0-3) 

**Attack path:**

1. At the start of epoch `E`, before `pool_data.last_updated_epoch` has been advanced to `E`, an attacker calls `advance_delegation_record` for a victim's `(stakeAccountPositions, publisher)` pair.
2. The on-chain program reads events up to `last_updated_epoch` (which is still `E-1`), computes zero or stale rewards, transfers them, and sets `delegation_record.last_epoch = E` (current epoch).
3. The victim's `delegation_record.last_epoch` is now `E`. No further call to `advance_delegation_record` for this pair can succeed until epoch `E+1`.
4. The victim loses the rewards that would have accrued during epoch `E` once `pool_data` is updated.

The first-call race is also present: a freshly created `DelegationRecord` with `last_epoch = 0` trivially passes any epoch check, so a griefer can race the staker's first legitimate claim.

---

### Impact Explanation

Loss of one epoch of OIS staking rewards per occurrence for the targeted staker. The attack is repeatable every epoch. When `y > 0` (the reward rate is non-zero), this constitutes a direct, quantifiable financial loss to stakers. The attacker bears only transaction fees (cheap on Solana).

Currently `y = 0` per OP-PIP-103, so immediate financial impact is zero, but the design flaw is present in the deployed contract and will become exploitable the moment `y` is set to a non-zero value via `update_y` (a privileged call by `reward_program_authority`): [5](#0-4) 

---

### Likelihood Explanation

**Moderate.** The attack requires:
- `y > 0` (not currently true, but a governance parameter that can be changed)
- Attacker to monitor epoch boundaries and submit a transaction before the victim or the pool-data updater

Solana epoch boundaries are predictable (~2–3 days). A bot can trivially race the first `advance_delegation_record` call of each epoch for targeted accounts. No privileged access is required.

---

### Recommendation

1. **Add a zero-reward guard on-chain:** Before advancing `delegation_record.last_epoch`, verify that the computed reward amount is `> 0`. If zero, revert with a descriptive error (e.g., `ZeroReward`).
2. **Initialize `last_epoch` to `current_epoch` at delegation record creation**, so the first call also respects the epoch cadence and cannot be front-run.
3. **Consider gating `advance_delegation_record` behind the staker's own signature** (i.e., require `owner` to sign), or at minimum require that `pool_data.last_updated_epoch == current_epoch` before advancing the record.

---

### Proof of Concept

```
Epoch E begins. pool_data.last_updated_epoch = E-1. y > 0.

Attacker tx:
  advance_delegation_record(
    payer = attacker,
    stakeAccountPositions = victim_account,
    publisher = some_publisher,
    ...
  )

Result:
  delegation_record.last_epoch = E  (advanced with 0 rewards, since pool_data not yet updated)

Victim tx (later in epoch E):
  advance_delegation_record(...)
  → FAILS: last_epoch == current_epoch, nothing to advance

Victim loses all rewards for epoch E.
Attack cost: ~1 Lamport tx fee per targeted account per epoch.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L140-347)
```json
    {
      "code": 6024,
      "name": "InvalidSlashCustodyAccount"
    }
  ],
  "instructions": [
    {
      "accounts": [
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
    {
      "accounts": [
        {
          "name": "payer",
          "signer": true,
          "writable": true
        },
        {
          "name": "stake_account_positions"
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
        },
        {
          "name": "stake_account_custody",
          "pda": {
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
          },
          "writable": true
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "name": "publisher_stake_account_positions",
          "optional": true
        },
        {
          "name": "publisher_stake_account_custody",
          "optional": true,
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "publisher_stake_account_positions"
              }
            ]
          },
          "writable": true
        },
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
    },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1153-1185)
```json
    {
      "accounts": [
        {
          "name": "reward_program_authority",
          "relations": ["pool_config"],
          "signer": true
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
          },
          "writable": true
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [
        {
          "name": "y",
          "type": "u64"
        }
      ],
      "discriminator": [224, 14, 232, 96, 41, 230, 183, 18],
      "name": "update_y"
    }
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1195-1202)
```json
      "name": "DelegationRecord",
      "type": {
        "fields": [
          {
            "name": "last_epoch",
            "type": "u64"
          },
          {
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L732-735)
```typescript
    // Filter out delegationRecord that are up to date
    const filteredPublishers = publishers.filter((_, index) => {
      return !(delegationRecords[index]?.lastEpoch === currentEpoch);
    });
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L739-757)
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
    );
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1653-1715)
```typescript
          {
            name: "lastUpdatedEpoch";
            type: "u64";
          },
          {
            name: "claimableRewards";
            type: "u64";
          },
          {
            name: "publishers";
            type: {
              array: ["pubkey", 1024];
            };
          },
          {
            name: "delState";
            type: {
              array: [
                {
                  defined: {
                    name: "delegationState";
                  };
                },
                1024,
              ];
            };
          },
          {
            name: "selfDelState";
            type: {
              array: [
                {
                  defined: {
                    name: "delegationState";
                  };
                },
                1024,
              ];
            };
          },
          {
            name: "publisherStakeAccounts";
            type: {
              array: ["pubkey", 1024];
            };
          },
          {
            name: "events";
            type: {
              array: [
                {
                  defined: {
                    name: "event";
                  };
                },
                52,
              ];
            };
          },
          {
            name: "numEvents";
            type: "u64";
          },
```
