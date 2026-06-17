### Title
`update_y` Changes Epoch Reward Rate Without First Advancing Epoch — Retroactive Reward Manipulation - (File: `governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The OIS integrity pool exposes an `update_y` instruction that modifies the global yield rate `y` stored in `PoolConfig` without first calling `advance` to settle the current epoch's reward event. Because `advance` reads `y` from `PoolConfig` at the moment it is called and writes it into the epoch's `Event` record, any change to `y` before `advance` is called for the current epoch causes the new rate to apply retroactively to the entire epoch rather than only to future epochs.

---

### Finding Description

The OIS integrity pool uses two separate on-chain instructions for epoch accounting:

**`advance`** — permissionless, callable by anyone. It reads `pool_config.y` and per-publisher caps, writes a new `Event{epoch, y, eventData[]}` into `pool_data.events`, and updates `pool_data.last_updated_epoch`. It can only advance one epoch at a time and is a no-op if `last_updated_epoch == current_epoch`. [1](#0-0) 

**`update_y`** — restricted to `reward_program_authority`. It writes a new value directly into `pool_config.y`. Its account list contains only `reward_program_authority`, `pool_config`, and `system_program` — **`pool_data` is absent**, so it cannot call `advance` internally. [2](#0-1) 

`PoolConfig.y` is the sole source of truth for the yield rate used when `advance` creates an epoch event: [3](#0-2) 

`PoolData.last_updated_epoch` tracks which epoch was last settled: [4](#0-3) 

The `Event` struct that is written by `advance` captures `y` at call time: [5](#0-4) 

**Vulnerable sequence:**

1. Epoch N begins. `advance` has not yet been called for epoch N (`last_updated_epoch == N-1`).
2. `reward_program_authority` calls `update_y(new_y)`. `pool_config.y` is now `new_y`.
3. Anyone calls `advance`. It reads `pool_config.y = new_y` and writes `Event{epoch: N, y: new_y, ...}`.
4. All stakers who were active during epoch N have their rewards computed against `new_y` for the **entire** epoch, even though `new_y` was set mid-epoch.

The same structural issue applies to `update_delegation_fee`, which writes directly to `pool_data.delegation_fees[]` without first advancing the epoch: [6](#0-5) 

`delegation_fees` is stored per-publisher in `PoolData` and is captured into `PublisherEventData.delegation_fee` when `advance` runs: [7](#0-6) 

---

### Impact Explanation

- **Loss or gain of yield to stakers**: If `y` is decreased mid-epoch before `advance`, stakers lose rewards they earned during the portion of the epoch before the change. If `y` is increased, stakers gain rewards they did not earn.
- **Retroactive delegation fee change**: A publisher's delegation fee change via `update_delegation_fee` before `advance` applies the new fee to the entire epoch, altering the publisher/delegator reward split retroactively.
- **Governance attack vector**: A malicious or compromised `reward_program_authority` can manipulate epoch rewards by timing `update_y` calls relative to `advance` calls — e.g., increasing `y` just before calling `advance` to inflate rewards, then decreasing it immediately after.

The `advance_delegation_record` instruction, which pays out rewards to individual stakers, reads from the settled `events[]` array: [8](#0-7) 

Once `advance` has written the incorrect `y` into an event, all subsequent `advance_delegation_record` calls for that epoch will use the wrong rate — the damage is permanent for that epoch.

---

### Likelihood Explanation

The OIS documentation states parameters are "captured at each start of the epoch." However, `advance` is a permissionless lazy call — it is not guaranteed to be called at epoch start before governance acts. The `reward_program_authority` (currently the Pyth DAO governance program) may submit a parameter-change proposal that executes mid-epoch without any protocol-enforced requirement to call `advance` first. The protocol provides no guard, no revert, and no ordering constraint between `update_y` and `advance`. [9](#0-8) 

---

### Recommendation

`update_y` and `update_delegation_fee` should call `advance` (or an equivalent internal epoch-settlement routine) before modifying `pool_config.y` or `pool_data.delegation_fees`. This mirrors the Reserve Protocol mitigation: settle rewards up to the current point before changing any parameter that affects reward computation.

Concretely, `update_y` should require `pool_data` and `pool_reward_custody` as additional accounts and invoke the advance logic inline before writing the new `y`, ensuring the new rate only applies to epochs that begin after the change.

---

### Proof of Concept

1. At the start of epoch N, `pool_data.last_updated_epoch = N-1` (advance not yet called).
2. `reward_program_authority` submits governance tx: `update_y(new_y)` where `new_y > old_y`.
3. Any user calls `advance` — it reads `pool_config.y = new_y` and records `Event{epoch: N, y: new_y}`.
4. All stakers call `advance_delegation_record` for epoch N and receive rewards computed at `new_y` for the full epoch.
5. Stakers who staked expecting `old_y` for epoch N receive inflated rewards; the reward custody is drained faster than intended.

Conversely, if `new_y < old_y`, stakers lose rewards they earned during the pre-change portion of epoch N with no recourse, since the event record is immutable once written. [2](#0-1) [1](#0-0)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L145-208)
```json
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
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L209-347)
```json
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

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1082-1119)
```json
    {
      "accounts": [
        {
          "name": "reward_program_authority",
          "relations": ["pool_config"],
          "signer": true
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
          "name": "delegation_fee",
          "type": "u64"
        }
      ],
      "discriminator": [197, 184, 73, 246, 24, 137, 184, 208],
      "name": "update_delegation_fee"
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

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1337-1362)
```json
      "name": "PoolConfig",
      "type": {
        "fields": [
          {
            "name": "pool_data",
            "type": "pubkey"
          },
          {
            "name": "reward_program_authority",
            "type": "pubkey"
          },
          {
            "name": "pyth_token_mint",
            "type": "pubkey"
          },
          {
            "name": "y",
            "type": "u64"
          },
          {
            "name": "slash_custody",
            "type": "pubkey"
          }
        ],
        "kind": "struct"
      }
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1365-1376)
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
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L1419-1450)
```json
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
        ],
        "kind": "struct"
      }
    },
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1297-1329)
```typescript
    {
      name: "updateY";
      discriminator: [224, 14, 232, 96, 41, 230, 183, 18];
      accounts: [
        {
          name: "rewardProgramAuthority";
          signer: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          writable: true;
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "systemProgram";
          address: "11111111111111111111111111111111";
        },
      ];
      args: [
        {
          name: "y";
          type: "u64";
        },
      ];
    },
```
