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

> ## ⏸ PENDING(2026-08-04 決議)
>
> **fiber_check application 暫時不會成為 AgentX subagent。本節以下的設計與工期估算
> 保留作參考,但不是目前的行動項目,不要據此排工。**
>
> 依據 `~/WORK/SNMP_50ms/plan-e-agentx-get-offload-design-2026-08-03.md`:
>
> - §4.4 只把 fiber_check 列為「**未來若有**第三個 daemon 註冊為 AgentX subagent
>   (PDF 規劃過 fiber_check / lldp / dhcp_snp)」的假設情境,用來論證 namelen 不變式;
>   Phase 0–8 的實際範圍裡**沒有** fiber_check daemon 變 subagent 這件事。
> - fiber_check 在 Plan E 的定位是 **Phase 7「dlmod GET 遷移」**:把 SNMP GET 從
>   dlmod plugin 導到 framework subagent。該 Phase 的 go/no-go 條件明列
>   「**fiber_check 延遲改善量測**」。
>
> **所以正確的順序是:等 Plan E Phase 7 把 fiber check 的 SNMP GET 導到 framework
> 之後再量測**,用當時的實測數字決定 fiber_check 還需不需要自己變 subagent。在那之前
> 本節的 ~50 µs / ~300x 都只是估算,沒有實測支撐。
>
> 量測前的基線見 §8。

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

> **本節已失效,見 §7.4 與 §2.3 的 PENDING 標註(2026-08-04)。**現行排序是:
>
> | # | 方案 | 狀態 |
> |---|---|---|
> | 1 | **A** | 已上線,但有 cross-process 缺陷 → 見 §7 |
> | 2 | **B**(既有 status SHM 加 `mIsPortLinkUp[]`)| **進行中** —— 不是 optional 加速,是修 A 的必要工作 |
> | 3 | C(fiber_check 變 AgentX subagent)| **⏸ PENDING** —— 不在 Plan E 範圍內;等 Phase 7 把 SNMP GET 導到 framework 後量測再決定 |

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

> **已失效(2026-08-04)**:上面這句「本來就在階段 2 PoC 範圍內」是根據
> `~/agentx_multi.md` 的舊規劃寫的。現行的
> `plan-e-agentx-get-offload-design-2026-08-03.md` 沒有把 fiber_check daemon 變
> subagent 列入 Phase 0–8;§4.4 只把它當「未來若有第三個 daemon」的假設情境。
> 表格裡那四條原則本身仍成立,但**不構成現在要做 C 的理由**。見 §2.3 的 PENDING 標註。

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

---

## 7. 方案 A 上線驗證(2026-08-03)— **A 有 cross-process 缺陷,已造成 production regression**

> 本節推翻 §2.1「半天搞定」與 §3「B 只在需要 sub-second sync 時才做」兩個結論。
> §2.1 風險段落講的「~5 s dump 週期落後」也不成立 —— 實測 `.stat` 是 on-demand 寫的,
> 根本沒有 polling 週期。

### 7.1 A 的實作狀態

兩個 repo 都已 commit 在 branch `snmp-plan-E2`:

| repo | commit | 內容 |
|---|---|---|
| `app_moxa_fiber_check` | `43138ba` "Act A: Quick win" | 加 `FiberCheck_IsPortLinkUp()`;`_OutputStatus()` 輸出 `linkStatus.%u` |
| `app_moxa_fiber_check` | `630a991` "Bug fix" | 把 `gIsPortLinkUp[]` 定義從 `fiber_check_main.c` 搬到 `fiber_check_shm_api.c` |
| `plugin_moxa_fiber_check` | `d277473` "Act A: Quick win" | `status.rs` 移除 `get_snmp_data_by_cli(ifTable)`(-33/+7),改讀 `linkStatus.{i}` |

`630a991` 的動機是 link error:`libmoxa_fiber_check.so` 只由 `shm_api.o / ddm.o /
defines.o / api.o` 組成,不含 `main.o`,所以 `43138ba` 讓 `.so` 連不起來。搬家解了
link error,但也讓 daemon 那份跟 dumper 那份徹底分家。

### 7.2 根因:`gIsPortLinkUp[]` 是 process-local,dumper 跟 daemon 不同 process

`.so` 裡的 global 不會跨 process 共享 —— 每個 map 它的 process 拿到自己一份歸零的 copy。
DUT 上實測(`/proc/*/maps`):

```
/proc/1087 fiber_check        ← daemon,唯一被 ISS event 更新的那份,但不 map .so
/proc/1227 app_moxa_framew    ← 實際跑 _OutputStatus() 的 process,它那份永遠 {false}
/proc/1302 snmpd
/proc/299  ISS.exe
```

daemon 的 Makefile 是把 `fiber_check_shm_api.o` 直接連進 `fiber_check` 執行檔,不是連 `.so`。
系統裡因此有**四份互不相干的 `gIsPortLinkUp[]`**。

寫 `.stat` 的是讀取方,不是 daemon:`status.rs:125` `get_status()` 第一行就呼
`mx_fiberCheck::output_shared_info()`。實測 `.stat` mtime 隨 `snmpget` 同秒跳動
(`1683046664 → 1683046739`),全 dl 樹 grep `FiberCheckShm_OutputShmInfo` 也確認
daemon 一次都沒呼叫。

### 7.3 End-to-end 證據(DUT 192.168.127.253,eth1/1 插 SFP-1GLSXLC-T 且 link up)

| 來源 | 欄位 | 值 |
|---|---|---|
| `ifOperStatus.1` | 實際 link | **1 (up)** |
| `.stat` | `linkStatus.0` | **2 (down)** ← 錯 |
| `.stat` | `modelName.0` | `SFP-1GLSXLC-T` |
| `.stat` | `txPower.0` | `-2.99` |
| `.stat` | **`rxPower.0`** | **`-1.92`** ← 值就在檔案裡 |
| SNMP col8 | `fiberCheckStatTxPower.1` | `-2.99` ✅ |
| SNMP col13 | `rxPowerLimit.1` | `[-19.00, -1.00]` ✅ |
| SNMP col9 | **`fiberCheckStatRxPower.1`** | **`N/A`** ❌ |

`.stat` 裡 `rxPower.0=-1.92` 是好的,是 `status.rs:58` 的 `if port_valid && link_up`
把它丟掉換成 `"N/A"`。`linkStatus` 讀不到時 `.unwrap_or(false)` 是 fail-closed。

**影響範圍精確到一個 object**:`fiberCheckStatRxPower`。所有不經過這個 gate 的欄位
(modelName / serialNumber / wavelength / temperature / voltage / txPower /
txBiasCurrent / 各種 Limit)都直接取自 SHM,正常。A 之前(走 ifTable 那版)是正確的。

SNMP 欄位對照(踩過的坑):col7=voltage、**col8=txPower、col9=rxPower**、col12/13=
txPowerLimit/rxPowerLimit(2 元素陣列)。§5 驗證腳本裡的 `...1.1.1.8.1` 是 txPower 不是
rxPower。

### 7.4 決策:B 從「optional 加速」改為「修 A 的必要工作」,範圍縮小

不採用的做法:

- **回退 A**(revert 三個 commit):會把 5–25 ms 的 ifTable call 加回來。既然 B 只要
  1–2 天,沒有理由先回退再重做。
- **在 framework process 內另外取得 link status**(例如讀 `/sys/class/net/*/operstate`
  或保留 ifTable call):等於繞回 A 想解決的問題,而且 fiber 邏輯 port 跟 netdev 不是
  一對一。
- **新開一塊 SHM segment + 專用 Rust binding crate**(文件 §2.2 原本估 2 週的做法):
  現成的 `kShmSrvBlkTypeFiberCheckStatusSrv` 已經在跨 process 傳同一份 port status,
  多開一塊沒有好處。

採用:把 link status 放進**既有**的 status SHM。

1. `include/fiber_check_monitor.h` `struct FiberCheckStatus` 加
   `bool mIsPortLinkUp[LIB_SYSTEM_DEF_MAX_PORT_NUM_IN_SYSTEM]`
2. daemon 在 `fiber_check_main.c`(ISS event / startup init)改呼叫
   `FiberCheckMntr_SetPortLinkUp()`,由它寫 `gFiberCheckStatus` 並 flush SHM
3. `fiber_check_shm_api.c:_OutputStatus()` 改讀
   `aFiberCheckStatus->mIsPortLinkUp[port]`;`gIsPortLinkUp[]` 與
   `FiberCheck_IsPortLinkUp()` 連同 `630a991` 搬家的那段一併移除
4. `status.rs` **不動**(它讀 `.stat` 的介面不變)

> **實作時的設計修正(2026-08-04)**:第 1 點原本寫的是「在
> `struct FiberCheckPortStatus` 加 `bool mLinkUp`」,實作時改為在
> `struct FiberCheckStatus` 內另開平行陣列。原因是讀完 `fiber_check_monitor.c`
> 才發現 `_ResetFiberDdmStatus()`(:80)與 DDM 更新(:219)兩處都是
> `memcpy(&gFiberCheckStatus.mPortStatus[aPortNo], &tmpStatus, ...)` 整個 port
> struct 覆寫 —— per-port 欄位會被每次 DDM refresh 與每次 SFP 移除 reset 清掉。
> 而且 link 狀態本來就跟 SFP 在不在無關,必須活過 SFP 移除。平行陣列讓這兩條既有
> 路徑完全不用動,也不留下「未來新增寫入路徑要記得保留 mLinkUp」的地雷。
>
> 連帶:`FiberCheckMntr_Main()` 起始的 `_InitSystemParameter()` 原本
> `memset(&gFiberCheckStatus, 0, sizeof(...))` 會清掉 link 狀態,而它跟 main task 的
> `_InitPortLinkStatus()` 是並行跑的。改成只 memset `mPortStatus` 成員,語意也更
> 精確(它本來就只負責 DDM 狀態初始化)。

副作用:同步變即時,§2.1 原本擔心的 dump 落後問題連同「dump 週期」這個錯誤前提一起消失。

### 7.5 ABI 相依性查證:ISS 不受影響,B 可單獨出版

| 檢查 | 結果 |
|---|---|
| 誰 include `fiber_check_monitor.h`(struct 所在) | 只有 `app_moxa_fiber_check` 自己的 monitor / shm_api / main 三個 `.c`,全 dl 樹無他人 |
| Makefile 對外安裝的 header | 只有 `fiber_check_api.h`,**不含** `FiberCheckStatus` / `FiberCheckPortStatus` |
| ISS 實際用到的 API | `FiberCheck_CheckEeprom / CheckVendorName / GetModelName / InitFiberEvent / NotifyFiberEvent`,均不碰 struct |
| ISS 有無引用 SHM key | `grep kShmSrvBlkTypeFiberCheckStatusSrv app_moxa_iss_10_1_0/` → 零命中 |
| SHM size 怎麼決定 | `fiber_check_main.c:912` `LibUtil_ShmCreate(..., sizeof(struct FiberCheckStatus))`,daemon 執行時決定 |

會 memcpy 這個 struct 的程式碼全部出自 `app_moxa_fiber_check` 同一次 build(daemon 執行檔
+ `.so`),必然同版本一起出。**不需要跟 ISS 同步升版。**

唯一注意事項:`shmget` 用既有 key 但更大 size 會回 EINVAL。正常韌體升級會 reboot、segment
消失所以沒問題;**熱換 binary 不重開機**的測試流程會踩到,要記得重開。

### 7.6 B 的驗收條件

沿用 7.3 的環境:

```bash
# eth1/1 link up 時
/usr/bin/snmpget -v2c -c public 192.168.127.253 .1.3.6.1.4.1.8691.603.5.3.2.1.1.1.9.1
# 預期:STRING: "-1.92"(或當下實際值),不是 "N/A"

# DUT 上對照
grep -E '^(linkStatus|rxPower)\.0=' /etc/moxa/app-moxa-fiber-check/fiberCheck.stat
# 預期:linkStatus.0=1  rxPower.0=-1.92

# 拔線後
# 預期:linkStatus.0=2,SNMP col9 回 "N/A"
```

### 7.7 對方案 C 的影響

無。C 讓 daemon 直接在自己 process 內讀 link status + status struct,本來就繞過這整個
cross-process 問題。B 是 C 落地前的過渡,兩者不衝突;C 上線後 `_OutputStatus()` 仍
保留給 REST/web UI 用。

**補充(2026-08-04)**:C 已標為 PENDING(§2.3),不在 Plan E 範圍內。這反過來讓 B
變成**唯一**會落地的修法 —— 原本「B 只是 C 之前的過渡」的說法不再成立,B 就是終局。

---

## 8. 量測基線(2026-08-04,Plan E2 build,Plan B 尚未上線)

Plan E Phase 7 把 fiber check 的 SNMP GET 導到 framework 之後要重新量測(§2.3),
以下是**導入前**的基線,供屆時對比。

來源:`~/pcap/2026-08-04_plan-E2_2026_0804_0824.pcapng`(17340 packets,1h52m,
DUT 192.168.127.253)。

**重要前提:`...1.1.1.8`(txPower)與 `...1.1.1.9`(rxPower)在這份 capture 裡從未被
分開查詢** —— 2640 個 request 全部是 2-varbind GET,同時帶兩個 column 的同一個 port,
共用一次 round trip。無法分別給出兩個 OID 各自的 latency。

| 指標 | 值 |
|---|---|
| n(request/response pair) | 2640(全部配對成功,無遺漏) |
| mean | **12.49 ms** |
| median | 10.73 ms |
| p90 / p95 / p99 | 14.53 / 27.81 / 30.56 ms |
| min | 10.25 ms |
| **worst case** | **47.60 ms** |
| stdev | 5.14 ms |
| **> 50 ms** | **0 / 2640(0.000%)** |
| > 40 ms | 4(0.152%) |
| > 30 ms | 30(1.136%) |

分布是雙峰的,原因是 `status.rs::get_cache_timeout_in_second()` 回傳 **5 秒**,而輪詢
週期是 ~30.6 秒:

| 樣本 | n | mean | max |
|---|---|---|---|
| 每輪第一個 request(cache miss) | 220 | **28.59 ms** | 47.60 ms |
| 同輪其餘 11 個(cache hit) | 2420 | **11.02 ms** | 45.45 ms |

輪詢形態:220 輪 × 12 port(port 1→12),輪內耗時 0.183 s,輪間隔 ~30.6 s。每輪第一發
必定 cache 過期,要重跑 `output_shared_info()`(讀 SHM + 寫 205 行 `.stat`)加 INI 解析
與整張 portTable 組裝;差值約 **17.6 ms** 就是這一整套的成本。(此因果是從資料形狀 +
程式碼推得,非直接量測;要坐實可把輪詢間隔改成 < 5 s 再抓一份,預期雙峰消失。)

**判讀**:

- 50 ms 目前有過,但餘裕僅 2.4 ms,且 worst case 全部落在 cache-miss 那一發。NMS 若改用
  低頻輪詢(每次都 miss),平均會從 12.5 ms 升到 ~28.6 ms,尾巴會很危險。
- 11 ms 的地板是 framework REST 這層的固定成本。**方案 B 不會改變它**(B 修的是正確性),
  屆時 Phase 7 的量測要比的是這個地板。

---

## Changelog

- 2026-06-02:從 status.rs trace 發現 ifTable 重量級 call 只為 link up/down 判斷。
  整理三方案(A 短期 / B 中期 / C 長期跟 AgentX PoC 整合)。
- 2026-08-03:A 上線驗證,發現 `gIsPortLinkUp[]` 是 process-local 而 `.stat` dumper
  跑在 framework process,導致 `fiberCheckStatRxPower` 恆為 "N/A"(regression)。
  查證 ISS 無 SHM ABI 相依。B 改為必要工作,範圍縮小為「既有 status SHM 加
  `bool mLinkUp`」。詳見 §7。
- 2026-08-04:B 實作完成(未建置驗證),設計改為平行陣列 `mIsPortLinkUp[]`,理由見
  §7.4。**方案 C 標為 PENDING**(§2.3)—— 依
  `plan-e-agentx-get-offload-design-2026-08-03.md`,fiber_check 不會成為 AgentX
  subagent,而是走 Phase 7「dlmod GET 遷移」導到 framework,屆時再量測決定。
  新增 §8 記錄導入前的 latency 基線(mean 12.49 ms / worst 47.60 ms / 無 >50 ms)。
