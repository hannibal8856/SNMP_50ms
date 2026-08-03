# fiber_check status — link status 取法優化

> 起因:trace `plugin_moxa_fiber_check/framework/src/status.rs` 時發現 fiber_check
> 為了在 RxPower 邊界顯示 "N/A" / 實值,跑了完整的 `get_snmp_data_by_cli` 到 ISS 拿
> 整個 28-row ifTable 只為了取每 port 的 `ifOperStatus`(link up/down)。
> 而 link status 的真正 source of truth 其實**就在 fiber_check daemon 自己手裡**。
>
> 短期改 1 行 C + 10 行 Rust 即可省 ~5–10 ms / GET。
> 中長期跟 ~/agentx_multi.md 階段 2 PoC 自然整合。

---

## 1. 現況問題

### 1.1 status.rs 的路徑

`plugin_moxa_fiber_check/framework/src/status.rs::_construct_port_table()`:

```rust
fn _construct_port_table(max_port_num_of_switch: u32) -> serde_json::Value {
    let mut config = Ini::new();
    config.load("/etc/moxa/app-moxa-fiber-check/fiberCheck.stat").unwrap();

    let mut linkstatus_table = vec![json!({}); max_port_num_of_switch as usize];

    /* (A) 跑整段 ifTable 從 ISS 拿 ifOperStatus —— 重量級! */
    let _if_table = SnmpDef {
        file_name: "ifTable",
        index_of_table: vec!["ifIndex"],
        value_type: vec![ValueType {
            name: "ifOperStatus",
            var_type: VarType::Int,
            ..
        }],
    };
    for iftable_obj in &get_snmp_data_by_cli(_if_table) {
        let port_idx = iftable_obj["table_index_dict"]["ifIndex"]
            .as_u64().unwrap() - 1;
        linkstatus_table[port_idx as usize] =
            iftable_obj["ifOperStatus"].clone();
    }

    /* (B) port_table 各欄位從同一份 INI 拿 */
    for i in 0..max_port_num_of_switch as usize {
        port_table[i]["temperatureC"] =
            config.get("status", &format!("temperatureC.{i}")).into();
        port_table[i]["txPower"] =
            config.get("status", &format!("txPower.{i}")).into();
        /* ...其他 12 個欄位都從 INI... */

        /* (C) link status 在這裡用 —— 唯一用途!*/
        if port_valid && [1].contains(&linkstatus_table[i].as_u64().unwrap_or(0)) {
            port_table[i]["rxPower"] = (從 INI 拿 rxPower);
        } else {
            port_table[i]["rxPower"] = json!("N/A");  /* link down → 隱藏 */
        }
    }
}
```

→ 整個 (A) 段跑 ISS CLI 只為了 (C) 那行 `if`。

### 1.2 (A) 段的實際開銷

`lib_moxa_rust_iss::get_snmp_data::get_snmp_data_by_cli()` 內部:

```
get_snmp_data_by_cli
  ↓ Command::new("/sbin/iss_cli_cmd") (fork + exec)
  ↓ 或 TCP connect 127.0.0.1:6023 (我們之前 TCP-direct patch 後)
  ↓ 送 ISS CLI "show interface table" 之類
  ↓ ISS 回 28 row × N column 文字
  ↓ Rust parse 成 Vec<JSON>
  ↓ 回
```

| 階段 | 時間 |
|---|---|
| fork + exec /sbin/iss_cli_cmd(原始)| 10–20 ms |
| 或 TCP 連 ISS port 6023(已 patched)| 1–2 ms |
| ISS 處理 + 回 28 row 文字 | 2–5 ms |
| Rust parse 28 row | 1–2 ms |
| **合計** | **5–25 ms** |

→ 每次 `/api/v1/status/fiberCheckStatus` GET 都吃這個。NMS 監控 1 Hz × 28 port → 1 秒 N 次 GET 累積到秒級延遲。

### 1.3 `max_port_num_of_switch` 不是問題

```rust
max_port_num_of_switch: module_info.max_port_num_of_switch()
                       /* lib_moxa_rust_iss::productinfo_module */
```

→ 從 `ModuleInfo::new()` 取的常數,**不打 SNMP,~1 µs**。沒問題。

### 1.4 諷刺的事實:fiber_check daemon 已經有 link status

`app_moxa_fiber_check/src/fiber_check_main.c`:

```c
/* line 64 */
static bool gIsPortLinkUp[LIB_SYSTEM_DEF_MAX_PORT_NUM_IN_SYSTEM] = { false };

/* line 442 — ISS event trigger 來時更新 */
gIsPortLinkUp[msgnode.mPortInfo.mLogicalIndex] = ...

/* line 833 — daemon 自己用 */
if (gIsPortLinkUp[port]) { ... }

/* line 959 — startup 用 URI 一次性 init,之後靠 event */
gIsPortLinkUp[i] = (json_object_get_number(obj, "linkStatus") == 1);
```

→ daemon 是 link status 的 **source of truth**。framework 卻繞回去問 ISS,等於問了 daemon 的「上游」拿同一份資訊。

---

## 2. 三個方案

### 2.1 方案 A:daemon 把 linkStatus 寫進 .stat INI(短期,推薦)

#### 改動

`app_moxa_fiber_check/src/fiber_check_shm_api.c`:

```c
/* 既有的 dump 函式內加一行 */
for (port = 0; port < max_port; port++) {
    /* 現有寫法 */
    fprintf(status_file, "modelName.%u=%s\n",  port, ...);
    fprintf(status_file, "temperatureC.%u=%.2lf\n", port, ...);
    /* ...12 個欄位... */

    /* 新增 */
    fprintf(status_file, "linkStatus.%u=%u\n",
            port, gIsPortLinkUp[port] ? 1 : 2);
}
```

`plugin_moxa_fiber_check/framework/src/status.rs`:

```rust
fn _construct_port_table(max_port_num_of_switch: u32) -> serde_json::Value {
    let mut config = Ini::new();
    config.load("/etc/moxa/app-moxa-fiber-check/fiberCheck.stat").unwrap();

    /* (A) 整段移除 —— 不再呼 get_snmp_data_by_cli */
    /* let _if_table = ... ; */
    /* for iftable_obj in &get_snmp_data_by_cli(_if_table) { ... } */

    for i in 0..max_port_num_of_switch as usize {
        /* 其他欄位不變 */

        /* (C) 從同一份 INI 拿 link status */
        let link_status: u64 = config
            .get("status", &format!("linkStatus.{i}"))
            .and_then(|s| s.parse().ok())
            .unwrap_or(2);  /* default down */

        if port_valid && link_status == 1 {
            port_table[i]["rxPower"] =
                config.get("status", &format!("rxPower.{i}")).into();
        } else {
            port_table[i]["rxPower"] = json!("N/A");
        }
    }
}
```

#### 工程量

- daemon C:**+1 行 fprintf**
- framework Rust:**-20 行(移除 ifTable 那段) + 改 5 行(換 link status 來源)**
- 測試:確認 link up → rxPower 有值;link down → rxPower 顯示 "N/A"
- **半天搞定**

#### 延遲改善

| | 現況 | 方案 A 後 |
|---|---|---|
| 取 link status | fork+exec → ISS → 28 row → parse | INI 多讀一個欄位 |
| 額外 process | 1 個(/sbin/iss_cli_cmd)| 0 |
| 額外網路 round-trip | 1(TCP to ISS 6023)| 0 |
| 延遲 | **5–25 ms** | **~10 µs**(跟其他 INI 欄位 amortized) |
| 改善幅度 | -- | **~500–2500x** |

#### 風險

- ISS event 跟 daemon SHM dump 之間有 race window:event 來時更新 `gIsPortLinkUp`,但下一次 dump 才寫進 .stat → 最多落後一個 dump 週期(~5 s,fiber_check daemon polling 週期)
- 影響:NMS 看到的 link state 可能慢 ~5 s。對監控應用 OK,對告警敏感場景要評估
- 緩解:daemon 在 link event 來時**立即 dump**(不等 polling 週期)

### 2.2 方案 B:daemon 開 SHM/Rust binding,framework 直接讀(中期)

#### 設計

`lib_moxa_rust_fiber_check` 加 API:

```rust
pub fn get_link_status(port: u32) -> Option<u8>;
pub fn get_link_status_all() -> Vec<u8>;
```

內部走 daemon 已經有的 SHM(or pipe / unix socket / dbus)拿 `gIsPortLinkUp[]`。

framework status.rs:

```rust
let link_table = mx_fiberCheck::get_link_status_all();
for i in 0..max_port_num_of_switch as usize {
    if port_valid && link_table[i] == 1 { /* up */
        port_table[i]["rxPower"] = (從 INI 拿);
    } else {
        port_table[i]["rxPower"] = json!("N/A");
    }
}
```

#### 工程量

- daemon SHM segment(或既有 SHM 加欄位):3–5 天
- Rust binding crate:2 天
- framework status.rs 改用 binding:1 天
- **~2 週**

#### 延遲改善

| | 方案 A | 方案 B |
|---|---|---|
| 延遲 | ~10 µs | **~1 µs**(直接讀 daemon SHM)|
| 同步延遲 | 5 s(dump 週期)| **即時**(SHM 隨時 read)|

#### 跟 A 比較

| | 方案 A | 方案 B |
|---|---|---|
| 工程量 | 半天 | 2 週 |
| 延遲 | 10 µs | 1 µs |
| 同步即時性 | ~5 s 落後 | 即時 |
| 對其他 consumer 友善 | 只有 framework 用 | SHM 任何 process 可讀 |

→ **方案 B 在「需要 sub-second link state」時值得**;否則 A 就夠。

### 2.3 方案 C:fiber_check daemon 變 AgentX subagent(長期,跟 ~/agentx_multi.md 階段 2 PoC 對接)

#### 設計

從 ~/agentx_multi.md 階段 2 PoC:fiber_check daemon 用 `lib_moxa_agentx` 變 subagent,自己 own `.1.3.6.1.4.1.8691.603.5.3.2.*`(status OID)。

```
GET fiberCheckStatRxPower.X 進來:
   ↓ snmpd master
   ↓ AgentX → fiber_check daemon (in-process subagent thread)
   ↓ handler:
     - 直接讀 gIsPortLinkUp[X]               ~1 µs
     - 直接讀 gFiberCheckStatus[X].mRxPower  ~1 µs
     - if up → return value; else → return N/A
   ↓ 回 AgentX → 回 client
```

#### 工程量

- 參考 ~/agentx_multi.md 階段 2 PoC,**3 週**(已涵蓋 lib_moxa_agentx + mapping lib + handler 接 SHM)

#### 延遲

| 路徑 | 延遲 |
|---|---|
| SNMP GET → 完整 fiber_check status portTable | **~50 µs**(整段 walk) |
| 同樣 GET 走現況(dlmod + framework + ISS) | ~15 ms |
| 改善幅度 | **~300x** |

#### 跟 A / B 比較

| | A | B | C |
|---|---|---|---|
| 改 daemon | 1 行 | SHM 改造 | 加 AgentX thread |
| 改 framework | 移除 ifTable call + 改 link 來源 | 改用 SHM Rust binding | status.rs 變成 REST 用 fallback,SNMP path 不再走它 |
| 改 snmpd | 無 | 無 | 拔掉 fiber_check.conf dlmod;加 agentx_owned.list 對應 prefix |
| 延遲改善(SNMP 路徑) | 比現況快 | 略快 | 最快(沒走 framework REST 那層) |
| 延遲改善(REST 路徑) | ✅ | ✅ | ✅(B/C 雖優化 SNMP,REST 透過 status.rs 也順帶受惠)|

#### C 對 SNMP path 的清掃效果

```
現況 dlmod 路徑(每個 fiberCheckStatTxPower.X GET):
   snmpd → plugin_snmp_fiber_check.so
     → HTTP /api/v1/status/fiberCheckStatus/...   (~7 ms)
     → framework status.rs::get_status()          (~10 ms)
       → fiber_check.stat INI                       (~1 ms)
       → get_snmp_data_by_cli for ifTable         (~5–25 ms)
       → 組 JSON 回
     → JSON 經 plugin 解出 single value
     → 回 SNMP response
   總: ~20–40 ms

方案 A 後:
   snmpd → dlmod
     → HTTP /api/v1/status/...
     → status.rs (no ifTable call)                 (~1 ms)
     → 回 JSON
     → 解 single value
   總: ~10 ms

方案 C 後:
   snmpd → AgentX → fiber_check daemon thread
     → 直接讀 gFiberCheckStatus[X] + gIsPortLinkUp[X]
     → 回
   總: ~50 µs

         (REST API /api/v1/status/fiberCheckStatus 維持 status.rs,
          web UI 仍可用,只是不是 SNMP 主路徑)
```

---

## 3. 排序建議

| # | 方案 | 何時做 | 主要好處 |
|---|---|---|---|
| 1 | **A**(.stat 加 linkStatus 欄位)| **立即**(半天)| 馬上拿掉 ifTable call,~500x 改善 REST 路徑 |
| 2 | C(fiber_check 變 AgentX subagent) | 階段 2 PoC(6 週後 go/no-go)| SNMP 主路徑 ~300x 改善;統一 AgentX 架構 |
| 3 | B(SHM Rust binding) | 只在「需要 sub-second link sync」時做 | 補 A 的 5 s 同步延遲 |

**A + C 是主路線**。B 只在實測發現 A 的同步延遲是瓶頸時補上。

---

## 4. 對 ~/agentx_multi.md 階段 2 PoC 的延伸啟示

這個 trace 進一步確認 ~/agentx_multi.md §4.3「daemon subagent 範本」的設計方向正確:

| 原則 | fiber_check 案例驗證 |
|---|---|
| **誰有資料,誰當 subagent** | daemon 有 `gIsPortLinkUp[]` + `gFiberCheckStatus[]`,理所當然 own status OID |
| **status vs config 分屬不同 subagent** | config OID(thresholds、mode)= framework owns;status OID(TxPower, RxPower, linkStatus)= daemon owns |
| **跨 daemon query 透過 AgentX in-process,不走 fork+exec / HTTP** | 現況跨到 ISS 拿 ifOperStatus 是 ~5–25 ms;同 daemon 內 in-process 拿 ~1 µs |
| **REST status.rs 保留**,作為 web UI fallback | 不影響 web UI,只是 SNMP path 改走 AgentX |

→ **方案 C 不是「額外」工作,本來就在階段 2 PoC 範圍內**。順序上:
1. PoC 起跑前 / 過程中,先做方案 A(獨立,半天)— 立即 production 受惠
2. PoC 內把 fiber_check daemon 改 subagent — 自然取代 A,SNMP 路徑大幅改善

---

## 5. 驗證 plan

### 方案 A 上線後

```bash
# 1. 看 .stat 真的有 linkStatus 欄位
cat /etc/moxa/app-moxa-fiber-check/fiberCheck.stat | grep linkStatus

# 2. 確認 status.rs 不再呼 ISS
# 啟 framework debug,看 log
journalctl -u app_moxa_framework -f | grep -E "get_snmp_data_by_cli|/sbin/iss_cli_cmd"
# 預期:跑 GET /api/v1/status/fiberCheckStatus 後不再出現

# 3. latency 對照
for i in $(seq 1 100); do
    /usr/bin/time -f "%e" curl -s \
        http://localhost/api/v1/status/fiberCheckStatus \
        >/dev/null 2>>/tmp/lat.txt
done
sort -n /tmp/lat.txt | awk '{a[NR]=$1} END {
    print "P50:", a[int(NR*0.5)];
    print "P99:", a[int(NR*0.99)];
}'
# 預期:從現況 ~15 ms → 方案 A 後 ~5 ms
```

### 方案 C(PoC)上線後

```bash
# SNMP path 直接量
for i in $(seq 1 100); do
    /usr/bin/time -f "%e" snmpget -v2c -c public localhost \
        SNMPv2-SMI::enterprises.8691.603.5.3.2.1.1.1.8.1 \
        >/dev/null 2>>/tmp/snmp_lat.txt
done
sort -n /tmp/snmp_lat.txt | awk '{a[NR]=$1} END {
    print "P50:", a[int(NR*0.5)];
    print "P99:", a[int(NR*0.99)];
}'
# 預期:現況 ~15 ms → C 後 < 1 ms
```

---

## 6. Reference

- 程式碼:
  - `plugin_moxa_fiber_check/framework/src/status.rs` (`_construct_port_table`)
  - `app_moxa_fiber_check/src/fiber_check_main.c:64`(`gIsPortLinkUp`)
  - `app_moxa_fiber_check/src/fiber_check_shm_api.c`(.stat 輸出位置)
  - `lib_moxa_rust_iss::get_snmp_data::get_snmp_data_by_cli`
- 相關規劃:
  - `~/agentx_multi.md` 階段 2 PoC(fiber_check 變 AgentX subagent)
  - `~/agentx_vs_proxy_truth.md`(AgentX 延遲量級對照)
- TCP-direct patch(降低 get_snmp_data_by_cli 開銷):
  - `lib_moxa_rust_iss/src/get_snmp_data.rs`,我們之前改的版本
- Vincent 2026 PDF Tier 3「in-process cache」:跟方案 C 同精神

## Changelog

- 2026-06-02:從 status.rs trace 發現 ifTable 重量級 call 只為 link up/down 判斷。
  整理三方案(A 短期 / B 中期 / C 長期跟 AgentX PoC 整合)。
