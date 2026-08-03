# Plan E — SNMP GET 卸載至 AgentX Subagent 設計文件

- 日期：2026-08-03
- 狀態：設計定案，待實作
- 取代：Plan C（`agentx_owned.list`）、Plan D（framework 全量 subagent）
- 相關文件：`SNMP Enhancement 2026`（Confluence 匯出 PDF）

---

## 1. 目標與範圍

### 1.1 目標

1. **延遲**：每個 SNMP GET 在 50 ms 內完成（與 eCos 時代 criteria 對齊）
2. **統一**：GET 路徑收斂成單一機制，消滅「config 走 framework / status 走 ISS / private 走 dlmod」三套並存
3. **相容**：SET 持久化模型完全不動，framework JSON 維持 source of truth

### 1.2 核心命題

> **SNMP GET 一律離開 snmpd，交給 AgentX subagent；SNMP SET 一律留在 in-master。**
> **唯一例外：值本來就在 snmpd 進程內、不需出 process 的 OID，GET 也留在 in-master。**

### 1.3 本次不做

- SET 路徑的任何改動
- SNMP trap 路徑（維持 MQTT → `app_moxa_trap_notification` → `/bin/snmptrap`）
- config layer / status layer 內部查詢效能優化（獨立 action item）
- Marvell CN9130 平台驗證（本階段以 **Cortex-A9** 為主，避免節外生枝）

---

## 2. 現況盤點

### 2.1 Repo 狀態

分支 `snmp-plan-E2` 存在於 `buildroot/moxa/` 與 7 個 dl repo：
`3rdparty_net_snmp`、`app_moxa_iss_10_1_0`、`plugin_moxa_snmp`、`app_moxa_framework`、
`app_moxa_fiber_check`、`plugin_moxa_fiber_check`、`lib_moxa_utility`。

已完成的前置工作：

| 位置 | commit | 內容 |
|---|---|---|
| `moxa/` | `d34da4b28` | net-snmp configure 加入 `agentx/master` |
| `moxa/` | `c08f5225b` | `app_moxa_iss_10_1_0` 加 `INSTALL_STAGING` |
| `plugin_moxa_snmp` | `config_moxa_snmp_control.c:792-796` | snmpd.conf 生成 `master agentx` / `agentXSocket tcp:127.0.0.1:705` |
| `dl/3rdparty_net_snmp` | `3bd5423`…`27f002e` | Plan C 原型（`agentx_owned.list` + probe-OID 轉發） |
| `app_moxa_iss_10_1_0` | `dac4ee673`、`aa621ff60`、`e6ee5205e` | AgentX 重連、snmpwalk 修正 |

> `plugin_moxa_snmp/app/script/init_snmpd.sh` 在 repo 中是 stub；執行期由
> `config_moxa_snmp_control.c:686` 重新生成為 `/bin/snmpd -f -C -c <conf> -p /var/run/snmpd.pid`。

### 2.2 OID 母體

`ies-auto-mibs/net_*.h` 共 **2136** 個 entry：

| 分類 | 總數 | RO | RW |
|---|---|---|---|
| `iss_build == 1`（ISS 供應） | 1391 | 934 | 457 |
| `iss_build == 0`（framework 供應） | 745 | 66 | 679 |

- `iss_build==1` 分佈：`8691.*` 323、LLDP(`1.0.8802`) 265、RMON(`.16`) 167、dot1dBridge(`.17`) 135、OSPF(`.14`) 115、IEEE802.1(`1.3.111`) 72、PROFINET 55…
- `iss_build==0` 分佈：741/745 在 `1.3.6.1.4.1.8691`，另 4 筆在 `1.3.6.1.6.3.10`
- **`iss_build==0` 的 679 個 RW 中，567 個（83.5%）是 `net_mx_event_*.h`** —— 事件（trap/syslog/email）組態表，非輪詢熱路徑

dlmod plugin（30+ 支 `.c`，OID 在 `8691.602.*` 與 `8691.603.5.*`）**完全不在 `net_*.h` 內**：

| 項目 | 量 |
|---|---|
| OID leaf（`OidScalar` 87 + `OidColumn` 59） | ~146 |
| 可寫 setter | 117 |
| 驗證邏輯 | ~370 |

### 2.3 現行 GET 的四條路（PDF 分類）

| Path | 觸發 | Backend |
|---|---|---|
| 1 | entry 有 in-process callback | snmpd 內 function call |
| 2 | `iss_build == 1` | `/sbin/ies_uri_handle_client` → ISS urihook → tmpfs 檔 |
| 3 | `iss_build == 0` | `LibFrameworkUri_GetUri` → nginx → framework |
| 4 | dlmod plugin `.so` | `lib_moxa_framework_uri` → nginx → framework |

**Path 1 的實測組成與 PDF 描述不符**：`__entryGetValueFunctionTable` 共 23 個註冊點，
其中只有 `net_mxGeneralInfo_setup.c` 的 5 筆是真正 in-process（呼叫 `LibSystem_*`）；
`mxLoginPolicy`(6) / `mxPasswordPolicy`(6) / `mx_snmp`(6) 共 18 筆
走 `getEntryValue_uriScalarEntry()` → `moxaSnmpHandle_UtilCfgGetURIConfig()` → nginx，
**是披著 Path 1 外衣的 Path 3**。

### 2.4 value-file 機制

Path 2/3 都不直接回值，而是先把資料寫成 tmpfs 上的**純文字檔**再讀回解析：

- 路徑：`/moxa/run/auto_mibs/<uri 的 / 換成 _>{_ent | tbl}`；`/moxa/run` 掛 tmpfs
  （`app_moxa_file_system/document/systemd/moxa-run.mount`）
- 格式：一行一筆 `OID = value`，`#` 為註解（解析見 `ies_auto_mibs.c:1918-1932`）
- 存在的原因是 **IPC 形狀所迫**：`ies_uri_handle_client --type read --uri X --file Y`
  沒有其他回傳管道
- 快取（`entry_handle_check_value_file`，`ies_auto_mibs.c:2010`）：
  scalar **永不快取**；table 只有單一全域 slot `gSnmpTblSnapShot`，逾時 800 ms / 200 ms

單筆 GET 的成本堆疊：fork+exec（或 HTTP→nginx）→ 產生整批資料 → 文字化 → 寫 tmpfs
→ open → 逐行 `fgets` → 字串比對 → 型別轉換。

### 2.5 基線量測（既有資料，`~/WORK/SNMP_50ms/snmpwalk/`）

對照組 `snmpwalk-NOS7-2026_0713_1621` vs Plan C `snmpwalk-agentx_owned.list-2026_0714_1515`：

| 子樹 | mainline | Plan C | 多出 | **缺少** |
|---|---|---|---|---|
| `1.3.6.1.2`（標準 MIB） | 1736 | 2219 | 483 | **0** |
| `1.3.6.1.4`（Moxa 私有，見下方涵蓋範圍警告） | 749 | 749 | 0 | **0** |

> **⚠ 既有基線的涵蓋範圍遠小於檔名所示，且揭露一個 mainline 既有缺陷。**
>
> 兩類 walk 檔的中止性質不同，必須分開看：
>
> | 檔案 | 最後一行 | 意義 |
> |---|---|---|
> | `snmpwalk-*-1.3.6.1.4.txt`（mainline 與 Plan C 皆同） | `End of MIB` | **agent 主動回 endOfMibView** |
> | `snmpwalk-*-…8691.603.txt`（mainline 與 plan-D 皆同） | 資料行，無 `End of MIB` | 人為中斷，測試範圍問題 |
>
> **(a) `1.3.6.1.4` walk 是 agent 說沒了，不是沒測。**
> 它在 `8691.602.5.1.1.8.0` 之後回報 endOfMibView，內容僅
> `8691.602.*`（527+50+43+13+2）、`8691.600.2`（5）、`2021.13.14`（109）。
> 但**同一次量測**（同 timestamp `2026_0713_1621`、同一台機器）另跑的 603 walk
> 證明 `8691.603.*` 有 802 個 OID 存在。
>
> → **GETNEXT 在 602/603 交界回了 endOfMibView。從 `1.3.6.1.4` 或 `.1` 起跑的完整 walk
> 永遠到不了 `8691.603.*`，NMS 全樹輪詢會漏掉所有 ISS 提供的 Moxa 私有 MIB。**
> mainline 與 Plan C 行為一致，屬**既有缺陷**，非 Plan C/D 造成。見 §12.3。
>
> **(b) 603 walk 是人為中斷。** 兩份都停在 `8691.603.3.2.2.1.1.16.12`（mxRSTP 中途），
> 只涵蓋 `603.1.1`(148)、`603.1.2`(49)、`603.2.3`(25)、`603.2.9`(324)、`603.3.2`(256)。
> `603.3.5` 之後（mxTc / mxDual / mxMcp / mxPhr / mxSup / mxMrp / mxAcl / mxLp /
> mxVa / mxLldp / mxPsms / mxPssp / mxMab …）**從未被比對過**；Plan C 側連 603 檔都沒有。
>
> 因此上表「私有樹 0/0」只成立於 `8691.602.*`（dlmod 地盤）。
> **`8691.603.*`（ISS / ies-auto-mibs 地盤，Plan E 的主戰場）尚無有效基線。**
>
> **Phase 0 的第一件事必須是修掉 (a) 並重建完整基線**，見 §10.1.1。

多出的 483 筆全在標準 MIB。**這些不全是洩漏——至少 26 筆是補回 mainline 缺失的必要物件**：

| | mainline | Plan C |
|---|---|---|
| `1.3.6.1.2.1.2.2.1.1`（**ifIndex**） | **零筆** | `[1..12, 130]` |
| `1.3.6.1.2.1.31.1.1.1.1`（**ifName**） | **零筆** | `[1..12, 130]` |

`ifIndex` / `ifName` 是 IF-MIB 的必要物件，**mainline 完全沒有曝露**。
推測原因是 `MAKEUP_INDEX_FOR_ISS`（旗標註解：「iss won't generate table index for mib_db,
so we have to makeup index for it」）對 ifTable 未生效。Plan C 把 col 1 交給 ISS 後即正常。

**同時證明 port index remapper 沒有被繞過**：ifTable 的 col 2–22 與 ifXTable 的 col 2–19，
mainline 與 Plan C 的 index 集合**逐欄完全相同**（含 `130` 這個非連續值）。
ISS 的 AgentX 路徑給出的 index 空間與 URI hook 路徑一致——
`remapper_util_check_data_port()` 的過濾/重編號未造成分歧。
（此結論只由 ifTable/ifXTable 實證，不可外推到其他群組，見 §9.1。）

其餘溢出多為 ISS 註冊整棵 root 帶出的節點：

```
240  1.3.6.1.2.1.17.6.1     pBridge
 72  1.3.6.1.2.1.17.2.19    dot1dStp 延伸
 29  1.3.6.1.2.1.17.7.1     qBridge
 36  1.3.6.1.2.1.17.4.4/5/6 dot1dTp
 26  1.3.6.1.2.1.31.1.2     ifStackTable
 26  1.3.6.1.2.1.2.2.1 + 31.1.1   ifTable / ifXTable 未實作欄位
 24  1.3.6.1.2.1.10.7.10/11 dot3
 20  1.3.6.1.2.1.4.24.7 / 4.35.1 / 4.28.1
```

### 2.6 溢出 OID 的分類結果（483 筆全部歸類）

判定依據：以 `snmp_moxa_mib/standard/*.mib` 解出各標準 MIB 的 root
（`BRIDGE-MIB ::= { mib-2 17 }`、`P-BRIDGE-MIB ::= { dot1dBridge 6 }`、
`Q-BRIDGE-MIB ::= { dot1dBridge 7 }`、`IF-MIB ::= { mib-2 31 }`、
`EtherLike-MIB ::= { mib-2 35 }`(dot3 在 `mib-2 10.7`)、`OSPF-MIB ::= { mib-2 14 }`、
`RMON-MIB ::= { mib-2 16 }`、`PTPBASE-MIB ::= { mib-2 241 }`），
再對照 `product/MDS-G4000-L3-4XGS.profile` 的 `### Standard MIBs` 清單。

#### (a) 合法補回 —— 382 筆

| 筆數 | arc | 所屬 MIB（profile 內） |
|---:|---|---|
| 240 | `1.3.6.1.2.1.17.6` | P-BRIDGE-MIB（dot1dExtBase 等） |
| 29 | `1.3.6.1.2.1.17.7` | Q-BRIDGE-MIB |
| 26 | `1.3.6.1.2.1.31.1.2` | IF-MIB（ifStackTable） |
| 24 | `1.3.6.1.2.1.10.7` | EtherLike-MIB（dot3） |
| 13 | `1.3.6.1.2.1.2.2.1.1` | IF-MIB **ifIndex**（RFC 必要物件，mainline 缺） |
| 13 | `1.3.6.1.2.1.31.1.1.1.1` | IF-MIB **ifName**（RFC 必要物件，mainline 缺） |
| 12 | `1.3.6.1.2.1.17.4.4` | BRIDGE-MIB（dot1dTp 4） |
| 12 | `1.3.6.1.2.1.17.4.5` | P-BRIDGE-MIB（dot1dTp 5） |
| 12 | `1.3.6.1.2.1.17.4.6` | P-BRIDGE-MIB（dot1dTp 6） |
| 1 | `1.3.6.1.2.1.14.2.1` | OSPF-MIB |

**這 382 筆全部屬於本型號 profile 已宣告的標準 MIB，也就是 Moxa 對外承諾要提供、
而 mainline 未提供的物件。** Plan E 的價值因此不只是延遲，還包括 **MIB 一致性**。

#### (b) 已實作但未宣告 —— 101 筆（**產品決策**，非技術缺陷）

**這 101 筆全部是標準 MIB 物件，ISS 依 RFC 正確實作，只是 Moxa 未 ship 對應的
MIB 模組、也未列入 profile。**不是私有垃圾 OID。

| 筆數 | arc | 標準物件 | 所屬 MIB | Moxa 是否 ship |
|---:|---|---|---|---|
| 74 | `17.2.{16,17,19}` | `dot1dStpVersion`、`dot1dStpTxHoldCount`、`dot1dStpExtPortTable`（含 `dot1dStpPortProtocolMigration` / `AdminEdgePort` / `OperEdgePort`…） | **RSTP-MIB（RFC 4318）** | ✗ |
| 13 | `4.24` | ipForward 群組 | **IP-FORWARD-MIB** | ✗ |
| 14 | `4.{25,26,27,28,29,33,35,38}` | ipv6 / ipNetToPhysical 等 | **IP-MIB** | ✗ |

ISS 端定義位置：`code/future/vlangarp/vlan/inc/stdbridb.h:31,82-86`。
`snmp_moxa_mib/standard/` 未 ship RSTP-MIB / IP-MIB / IP-FORWARD-MIB / UDP-MIB / TCP-MIB。
profile 走的是 IEEE 路線（`IEEE8021-SPANNING-TREE-MIB=YES`、`IEEE8021-MSTP-MIB=YES`），
RFC 4318 的 dot1dStp 延伸是另一條未宣告的路徑。

**兩個選項，須由 RD / PM 裁決，本設計不預設立場：**

- **關閉**：ISS 端不註冊（優先）或 VACM exclude。現行已排除 `4.31` / `4.32`
  （`config_moxa_snmp_control.c:875-876`），延用同一手法即可涵蓋其餘 ip arc
- **補文件**：ship 對應 `.mib` 並加入 product profile，把既有實作正式化

#### (c) Moxa 私有 arc、已實作但未宣告 —— `mxAcl` / `mxVa`

與 (b) 同性質，但落在 Moxa 私有樹，見 §9.2。ISS 有完整 MIB-DB 與 Get/Set handler：

| MIB | ISS 定義 | 規模 |
|---|---|---|
| `mxAcl`（`8691.603.4.7`） | `code/future/ISS/{bcm,xcat,xcat_v11}/inc/mxAcldb.h`，`mxAclOID = {10, mxAcl}` | 58–62 個 OID（依 MAC SDK；本產品 `BR2_MOXA_MAC_SDK="CPSS"`） |
| `mxVa`（`8691.603.4.15`） | `code/future/va/inc/mxVadb.h:13-30`，`mxVaOID = {10, mxVa}` | 3 個物件（global enable + port table） |

同樣是「關閉 or 補 `.mib` + 加入 profile」的產品決策。
基線未涵蓋此 arc（§2.5），須先取得完整 walk 確認實際回值。

#### 本分類的限制

- 「在 profile 內」代表**該 MIB 模組**為本型號宣告曝露，不等於逐一物件都應曝露；
  269 筆的 P-BRIDGE / Q-BRIDGE 大宗建議再由 RD 確認一次
- 本分類只比對 **OID 是否出現**，**未驗證值是否正確**。值的正確性屬各 phase 的硬門檻
- 分類基於 `1.3.6.1.2` 的 walk；該 walk 兩側都停在 `…241.1.2.9.1.6.0.0.1.12`，
  比對對等但未必涵蓋整棵標準樹（§2.5）
- **不可由「找不到 Moxa 側服務者」推論為洩漏**。ISS 常有完整實作，
  缺的只是 Moxa 的 `.mib` 與 profile 宣告。初稿犯過此錯，已修正

---

## 3. 為什麼 Plan C 的機制撐不住規模

Plan C 有兩個機制，都是為 ifTable 量身打造，無法通用化。

### 3.1 `agentx_owned.list`：key 與路由空間不對應

清單比對 `entry->uri`（`ies_auto_mibs.c:3400`），但路由發生在 **OID 空間**。

反例 `mxLadb`：OID 是 `1.3.6.1.4.1.8691.603.1.2.*`（Moxa 私有），但 `.uri` 是
`fscfadb/ifMainTable/`、`fsladb/fsLaPortChannelTable/`（ISS 自己的 MIB 名，靠
`ISS_REMAP_TBL` 轉檔名）。ISS 端 `fsladb` 掛在 `enterprises.2076` 下，而
`SnxMainRegisterMibs()`（`snxmain.c:906-918`）已被明文改成**跳過 2076**。
把 `fsladb/` 放進清單的結果是那些 OID 變成 `noSuchObject`，不是變快。

### 3.2 「只 offload RO」的規則在 framework 側失效

| | RO | RW | RO 佔比 |
|---|---|---|---|
| `iss_build==1` | 934 | 457 | 67% |
| `iss_build==0` | 66 | 679 | **9%** |

### 3.3 合成 index entry 影子覆蓋（24 張表）

`mox_snmp_init_entry()` 的順序是 `agentx_owned skip` → `GenerateTableIndexEntry` → `RegisterToNetSNMP`。

`moxaSnmpHandle_UtilMibEntryGenerateTableIndexEntry()`（`moxa_snmp_handle_util.c:325`）
用 function-static `tableURI` 當「本表 col 1 已見過」的閂：見到 col 1 記下 URI；
見到 col 2 且閂未設，就 `CreateIndexEntry()` 複製 entry、**把最後一個 OID element 改成 1**、
設 RONLY 並本地註冊（`moxa_snmp_handle_util.c:166-168`）。

col 1（RO index）被 skip 後閂不會設，col 2 若為 RW 就會憑空生出本地 col-1 handler，
蓋住要交給 ISS 的那一欄。符合「col1=RO 且 col2=RW」的 `iss_build==1` 表共 **24 張**：

```
mxDualdb/dualhomingConfigTable       mxMabdb/mabConfigPortTable
mxOspfdb/ospfConfigIfTable           mxOspfdb/ospfConfigRrdTable
mxPortdb/portConfigTable             mxPortdb/portConfigLinkUpDelayTable
mxPsmsdb/psmsConfigPortTable         mxPsspdb/psspConfigPortTable
stdbridb/dot1dStpPortTable           stdipdb/ipNetToMediaTable
stdospdb/ospfAreaTable               stdpnadb/dot1xSuppConfigTable
stdvladb/dot1qForwardAllTable        stdvladb/dot1qForwardUnregisteredTable
stdrmodb/{alarm,bufferControl,channel,etherStats,event,filter,
          historyControl,hostControl,hostTopNControl,matrixControl}Table
```

ifTable 未踩到純屬運氣：其 col 2（ifDescr）也是 RO，一起被 skip，未進入
`2 == GetTableEntryOID(entry)` 分支。

### 3.4 probe-OID 轉發無法通用化

`mox_snmp_forward_get_to_iss()`（`ies_auto_mibs.c:3144`）用一個「ISS 獨佔且本地未註冊的
RO instance」當 probe，讓 `netsnmp_subtree_find()` 直接落在 ISS 身上。
probe 的存在與否是每張表的偶然性質，硬編碼 C 陣列目前僅 4 條，不可能擴到數百條。

---

## 4. 目標架構

### 4.1 歸屬三分法

| 桶 | GET | SET | 內容 | 量 |
|---|---|---|---|---|
| **A** | ISS AgentX | in-master | `iss_build==1` 全部（不分 RO/RW）+ 新建 mxLa / mxVlan / mxLldp wrapper | 1391 |
| **B** | framework AgentX | in-master | `iss_build==0` 扣掉桶 C 之後的全部（**含**被誤歸為 Path 1 的 `mxLoginPolicy`/`mxPasswordPolicy`/`mx_snmp` 共 18 筆）+ 全部 dlmod OID | 745 − 6 = 739，加 dlmod |
| **C** | in-master | in-master | 自 `iss_build==0` 中劃出：`mxGeneralInfo` 5 筆（走 `LibSystem_*`，真 in-process）、`mx_eventLog` 的 `URI_FIXED_VALUE` 1 筆 | 6 |

桶 C 另加 `stdsnmdb` 的 sysObjectID（原以 `GENERATE_SYS_OID` 走 ISS value-file 路徑，
Phase 8 改為收進 `__entryGetValueFunctionTable`）。

`1.3.6.1.6.3.10` snmpEngine 4 筆**移除**，交還 net-snmp 原生模組（見 §8.3）。

### 4.2 機制一：整段 OID 註冊

in-master 對每個遷移中的 MIB 改用**整段註冊**（FRR `register_mib` 風格；
表用 `netsnmp_register_table_iterator2`），取代現行逐欄位 `netsnmp_register_handler`。

**副作用**：`GenerateTableIndexEntry` 的「col-1 缺席就合成」邏輯在結構上消失，
§3.3 的 24 張表風險**不需另外修就解掉**。

**必要條件**：priority 必須小於 127。ISS 送出的 Register-PDU 用
`SNX_DEF_REG_PRIORITY = 127`（`snxdef.h:85`），net-snmp 本地註冊預設也是
`DEFAULT_MIB_PRIORITY = 127`（`agent_registry.h:91`）。整段註冊後兩者 OID 與 namelen 相同，
priority 再相同就會 `MIB_DUPLICATE_REGISTRATION`，後註冊者失敗。**建議 priority = 100。**

但 priority 只解決 namelen 相同的情形，**不足以保護 in-master**，見 §4.4。

### 4.3 機制二：`->children` 鏈轉發（取代 probe-OID）

#### 註冊表的實際形狀

net-snmp 的註冊表是二維的：

- **`->next`/`->prev`（水平）**：把 OID 空間切成互不重疊的區段，GETNEXT 走這條
- **`->children`（垂直）**：同一區段上所有重疊的註冊，勝者到敗者，**敗者不會被丟棄**

排序規則在 `netsnmp_subtree_load()` case 0（`agent_registry.c`）：

```c
while (next && next->namelen > new_sub->namelen)      { prev = next; next = next->children; }
while (next && next->namelen == new_sub->namelen &&
       next->priority < new_sub->priority)            { prev = next; next = next->children; }
if (next && next->namelen == new_sub->namelen && next->priority == new_sub->priority)
        return MIB_DUPLICATE_REGISTRATION;
```

排序鍵 = **(`namelen` 由長到短, `priority` 由小到大)**。

`netsnmp_subtree_split()` 用 `netsnmp_subtree_deepcopy()` 複製，只改 `start_a`/`end_a`，
**`name_a`、`namelen`、`reginfo` 原封不動**。因此 subagent 的廣註冊被本地註冊切開後，
切片仍帶原本的 `namelen` 與 `agentx_master_handler` reginfo，掛在同一條 children 鏈上。

#### 轉發實作

```c
static int
mox_snmp_forward_get_to_subagent(netsnmp_agent_request_info *reqinfo,
                                 netsnmp_request_info       *request)
{
    netsnmp_subtree      *sub, *c;
    netsnmp_request_info *saved_next;

    sub = netsnmp_subtree_find(request->requestvb->name,
                               request->requestvb->name_length,
                               NULL, reqinfo->asp->pdu->contextName);
    if (sub == NULL)
        return -1;

    for (c = sub->children; c != NULL; c = c->children)
        if (c->reginfo && c->reginfo->handler &&
            c->reginfo->handler->access_method == agentx_master_handler)
            break;

    if (c == NULL)
        return -1;                    /* 無 subagent 覆蓋 → 回落本地慢路徑 */

    saved_next    = request->next;    /* 只轉發這一筆，不拖走整條 chain */
    request->next = NULL;
    (void) netsnmp_call_handlers(c->reginfo, reqinfo, request);
    request->next = saved_next;
    return 0;
}
```

- `agentx_master_handler` 宣告於 `agent/mibgroup/agentx/master.h:12`
- `c->reginfo->handler->myvoid` 是 `netsnmp_session *`（`master_admin.c:230`），可區分 subagent
- **這是 net-snmp 自己的慣用法**：`netsnmp_unregister_mib_context()`
  （`agent_registry.c:1705-1719`）用完全相同的 children 走訪找特定註冊

#### `agentx_owned.list` 廢除

路由變成結構性的：整段註冊 + 無條件 GET 轉發。不再有清單要維護，也不會有
「清單與 ISS 實際註冊不同步」的問題。**額外好處**：subagent 斷線時 `c == NULL`，
自動回落本地 value-file 路徑（現況是 RO OID 直接從 MIB 樹上消失）。

#### GETNEXT 邊界

`MODE_IS_GET(x)` 定義為 `(x >= 128) && (x != -1)`（`snmp_agent.h:143`），
**包含 GETNEXT 與 GETBULK**。轉發 GETNEXT 時，subagent 在其註冊區段內找下一個 OID；
若 subagent 區段比 in-master 註冊寬，回傳 OID 可能落在區段外，造成 walk 重複或倒退。

現行 4 條規則未爆是因 ifTable 形狀剛好對得上。**改成整段註冊後此風險消失**：
in-master 註冊範圍與 subagent root 一致，轉發回來的 OID 必在同一區段內。
這是機制一的第二個結構性好處。

**驗收要求**：機制二完成時必須做 snmpwalk 與 NOS mainline 交叉比對，確認 walk 結果一致。

### 4.4 多 subagent 的註冊不變式

未來若有第三個 daemon 註冊為 AgentX subagent（PDF 規劃過 fiber_check / lldp / dhcp_snp），
**priority 不是關鍵，`namelen` 才是**。排序鍵是 (`namelen` 由長到短, `priority` 由小到大)，
`namelen` 先比，priority 只在 namelen 相同時才起作用。

| 情境 | children 鏈 head | 結果 | priority 有效？ |
|---|---|---|---|
| 新 subagent 的 OID 範圍與 in-master **不重疊** | subagent | 該範圍 GET+SET 都歸 subagent，違反「SET 一律 in-master」 | 無關 |
| 新 subagent 的註冊 root 與 in-master **等長** | in-master（prio 100 < 127） | 設計成立 | **有效**（唯一起作用之處） |
| 新 subagent 註冊得**比 in-master 細**（OID 更長） | **subagent** | **in-master 被完全繞過**：SET 送到 subagent、GET 轉發不執行 | **無效** |

第三種是地雷。例：in-master 註冊 `1.3.6.1.2.1.2`（namelen 7, prio 100），
新 subagent 只註冊 `1.3.6.1.2.1.2.2.1.8`（namelen 10, prio 127）：

```
1. new subagent  namelen=10  prio=127   ← head，in-master 永遠不會被呼叫
2. in-master     namelen=7   prio=100
3. ISS           namelen=7   prio=127
```

把 in-master 的 priority 調到 0 也救不回來。

#### 不變式（實作必須維持）

> **in-master 在每一個 subagent 註冊覆蓋的 OID 範圍上，必須有 `namelen ≥` 該 subagent 的註冊。**

只要這條成立，**多個 subagent 疊加本身沒有問題**。例如 ISS 註冊整個 interfaces group、
新 daemon 只註冊 `ifOperStatus`，而 in-master 兩段都有等長註冊時：

```
1. in-master(10, 100)      ← head，SET 與轉發都正常
2. new subagent(10, 127)   ← 轉發取到的第一個 AgentX，較細者優先，語意正確
3. in-master(7, 100)
4. ISS(7, 127)
```

`mox_snmp_forward_get_to_subagent()` 取 children 鏈上第一個 AgentX 節點，
自然就是粒度最細的那個，**不需要額外判斷是哪個 subagent**。

#### 偵測

`SNMPD_CALLBACK_REGISTER_OID`（`agent_callbacks.h:5`）在 `agent_registry.c:1285,1322` 觸發；
AgentX 註冊也走同一條 `netsnmp_register_mib()`（`master_admin.c:241`），因此會被攔到。
payload `struct register_parameters`（`agent_registry.h:37-48`）帶
`name` / `namelen` / `priority` / `session` / `reginfo` / `contextName`，足以判斷。

in-master 掛一個監聽器，收到 AgentX 註冊時檢查自身是否有等長或更長的覆蓋，
違反則記 error log（或自動補一個等長的本地註冊）。這比靠文件約定可靠，
且新 daemon 上線時若註冊粒度不對，開機 log 就會指出來。

---

## 5. 模組邊界

| 模組 | 現在 | Plan E 之後 |
|---|---|---|
| `3rdparty_net_snmp/ies-auto-mibs` | OID 的 GET + SET 實作 | **SET 實作 + GET 轉發器 + OID/URI mapping 真相來源**；逐欄位註冊改整段；刪 probe 表與 `agentx_owned.list` |
| `app_moxa_iss_10_1_0` | AgentX subagent（**37 個** Moxa private MIB root，全部已 `SNMPRegisterMib`；另有標準 MIB） | 新增 mxLa / mxVlan / mxLldp wrapper；收斂多餘註冊（§9.3） |
| `app_moxa_framework` | actix-web REST 伺服器 | **兼任 AgentX subagent** |
| `lib_moxa_ies_auto_mibs`（新，C） | — | OID → URI mapping 唯一真相來源 |
| `lib_moxa_rust_ies_auto_mibs`（新） | — | 上者的 Rust FFI |
| `lib_moxa_snmp_agentx`（新，C） | — | AgentX 連線/註冊狀態機 + net-snmp 型別收斂 |
| `lib_moxa_rust_snmp_agentx`（新） | — | 上者的安全封裝 |
| `lib_moxa_snmp_plugin` | snmpd 內 dlmod helper | backend 抽象化，可在 snmpd 與 framework 兩處編譯 |
| `plugin_moxa_*/snmp/*.c` | dlmod `.so`（GET+SET） | 同一份原始碼編兩次：snmpd 側只用 SET，framework 側只用 GET |
| `plugin_moxa_snmp` | 生成 snmpd.conf | 不變（移除 `agentx_owned.list` 安裝） |

### 5.1 `lib_moxa_ies_auto_mibs`

單一職責：**給定 OID，回答「對應哪個 URI、走哪一層、要做哪些語意轉換」**。

- 編譯期 `#include` `3rdparty_net_snmp/ies-auto-mibs/net_*.h`（那些 header 內是陣列**定義**），
  在自身 `.c` 展開成 runtime 查表；**不複製、不轉檔**
- 對外只暴露 `iesmap_lookup(oid, len, &result)`，result 含
  `{uri, mod_uri, layer, val_type, oid_index, flags}`
- 不含 net-snmp handler、不含 SET 邏輯、不開 socket

**建置需求（必須遵守）**：`net_*.h` 內有大量 `#if defined(MOXA_*)` 條件編譯
（見 `3RDPARTY_NET_SNMP_CFLAGS`）。`lib_moxa_ies_auto_mibs` **必須拿到與 snmpd 完全相同的
那組 macro**，否則兩邊 entry 集合不一致。

### 5.2 `lib_moxa_snmp_agentx`：封裝邊界而非 API 糖

`agent/Makefile.in:141` 是 `LMIBOBJS = $(mibgroup_list_lo) mib_modules.lo auto_nlist.lo`
—— **所有 mibgroup 都進 `libnetsnmpmibs`**，其中就包含 ies-auto-mibs 本身
（連同 parson / `lib_moxa_framework_uri` / `lib_moxa_system` / ISS remap 的相依）。
FRR 用 `net-snmp-config --agent-libs` 連 `libnetsnmpagent` + `libnetsnmpmibs`；
Moxa 若照抄，等於把整個 ies-auto-mibs 拖進 framework 進程。

但 AgentX subagent 需要的很少。`agentx/subagent.h` 的 `config_require` 只有三個：
`agentx/protocol`、`agentx/client`、`agentx/agentx_config`，即
`subagent.c` / `protocol.c` / `client.c` / `agentx_config.c` 四個檔（外加 Callback transport）。

**`lib_moxa_snmp_agentx` 連 `libnetsnmpagent` + `libnetsnmp` + 那四個 agentx 物件，
把 `libnetsnmpmibs` / ies-auto-mibs 擋在 framework 之外。這一層非做不可。**

第二個理由：net-snmp 的 handler 簽章與 `netsnmp_request_info` 內有大量 union 與
`snmp_set_var_typed_value` 這類巨集/inline，bindgen 產出脆弱。用薄 C 收斂成純量 API
後再 FFI，Rust 端只面對 primitive。

**建置變更**：`.mk` 的 `--with-mib-modules` 目前只有 `agentx/master`，需加
`agentx/subagent`（加了對 snmpd 無害，subagent 模式由執行期 `-X` 旗標啟用）。

### 5.3 framework subagent 的進程內整合

比照 FRR（`~/SRC/frr/lib/agentx.c`）：

```
netsnmp_enable_subagent(); init_agent("moxa-framework"); init_snmp("moxa-framework");
register_mib(...) / netsnmp_register_table_iterator2(...)     ← 整段註冊
snmp_select_info2(&maxfd, &lfds, &timeout, &block)            ← 取出 net-snmp 的 fd
  → 掛進 tokio（AsyncFd），可讀時 snmp_read2() + netsnmp_check_outstanding_agent_requests()
```

**不開專屬 thread**（FRR 也不開）。net-snmp library 非 thread-safe，所有 net-snmp 呼叫
限制在單一 task 內；handler 所需資料透過既有 `AppState`（`Arc<Mutex<...>>`）取得。

AgentX 重連由 net-snmp 原生 subagent 模組提供（`agentx_check_session` alarm + ping），
不必重造 —— ISS 為此付過代價（`dac4ee673`、`aa621ff60` 兩個 commit）。

---

## 6. 資料流

### 6.1 GET

```
                        snmpd (net-snmp master)
                        USM 驗證 → VACM 授權
                                 │
                    netsnmp_subtree_find(oid)
                                 │
                    in-master 整段註冊 handler（priority 100，勝出）
                                 │
                       MODE_IS_GET(reqinfo->mode)?
                    ┌────────────┴────────────┐
                   是                         否 → SET 流程（6.2）
                    │
        走 sub->children 找 agentx_master_handler
        ┌───────────┴───────────┬──────────────────┐
     ISS session          framework session      找不到
        │                       │                   │
   AgentX(tcp:705)         AgentX(tcp:705)      本地 value-file 慢路徑
        │                       │                   （降級 fallback）
   ISS 進程內                framework 進程內
   nmhGet* / MIB DB        config_layer / status_layer
                           （in-process，不經 nginx）
```

桶 C 的 OID 不轉發，handler 直接在 snmpd 內取值。

### 6.2 SET —— 完全不變

```
SET PDU → in-master handler → MODE_IS_SET
  MODE_SET_RESERVE1 → moxaSnmpHandle_ChkEntryDep()
  MODE_SET_COMMIT   → moxaSnmpHandle_ExeEntrySetValue() / mox_snmp_flask_handle_entry_set()
                    → LibFrameworkUri_PatchUri(handle, uri, security_token, ...)
                    → nginx → framework config_layer
                    → POST /action/save + sleep(MOXA_SNMP_MAX_SET_DELAY)
```

`READ_CREATE_v2` / `_vRMON` / `_OID_URI` 的 rowStatus / createAndGo 邏輯**一行不動**。
24 個 rowStatus 欄位全部是 `HANDLER_CAN_RWRITE`，永遠留在此路徑。

### 6.3 身分與權限

> **GET**：VACM 在 master 把關 → 轉發 subagent → subagent **無身分**讀取，回值。
> **SET**：完全不動，`security_token` / per-user token / 權限模型全保留。

AgentX PDU 不帶 `securityName`（RFC 2741 既成事實；`master.c` 的
`agentx_master_handler` 建 PDU 時只放 version/reqid/transid/sessid/community/flags），
subagent 在協定上就拿不到發起者身分。

因此 framework subagent **不持有任何 token**。身分與權限的複雜度被完全關在
SET 那條沒動過的路裡，GET 路徑上不存在身分概念，也就不會有身分相關的迴歸風險。

SET 側保留 `ies_auto_mibs.c:371` 為每個 SNMPv3 rwuser 鑄的 `snmp.<username>` token，
以及 `ies_auto_mibs.c:3218-3230` 依 `securityName` 選 token 的邏輯。

---

## 7. 隨 OID 遷移的語意（桶 B 最易遺漏）

`entry_handle_generate_uri_value_file()`（`ies_auto_mibs.c:2370-2569`）在 GET 時做的事，
framework subagent 必須原樣重現，否則值會變。這是遷移的**驗收清單**。

| 語意 | 作用 | `iss_build==0` 設定點 | 去向 |
|---|---|---|---|
| `URI_STATUS` | 決定 `/api/v1/status/<uri>` 或 `/api/v1/setting/data/<uri>` | 11 | framework：選 status_layer 或 config_layer |
| `REPLACE_TRUTH_VALUE` | MIB enable(1)/disable(2) ↔ JSON `true`/`false` | **75** | framework |
| `TR2_REPLACE_URI` | TurboRing2：改用 `mod_uri` | 6 | framework |
| `URI_MOD_BY_IDX` | 依 index 改寫 URI | 2 | framework |
| `URI_INTERFACE` | port index 0-base(URI) ↔ 1-base(SNMP) | 1 | framework |
| `URI_FIXED_VALUE` | 本地固定值，不出 process | 1 | **留 in-master（桶 C）** |
| `mod_uri` | MIB 名 → config key 的人工橋接 | 7 個設定點 | framework（由 `lib_moxa_ies_auto_mibs` 提供） |

75 個 `REPLACE_TRUTH_VALUE` 幾乎全在 `mx_event_*`（port 30、switching 15、
turboring_v2 6、routing 5、dhcpSrv 4、portmirror 4…），實作上是**一條規則不是 75 條**。

### 7.1 桶 A 不需要搬語意

ISS GET path（`entry_handle_generate_iss_value_file`，`ies_auto_mibs.c:2182-2369`
→ `entry_handle_get_value_from_file`，`:1879-2009`）**不做任何值轉換**，
只認 4 個 flag：`ISS_REMAP_TBL`、`ISS_REMAP_MERGE_TABLE`、`MAKEUP_INDEX_FOR_ISS`、
`GENERATE_SYS_OID`。前三個在 AgentX 下無意義（ISS 以 OID 定址、自己給 index）；
`GENERATE_SYS_OID` 是 snmpd 自算的 sysObjectID，見 §8.2。

`mod_uri` 亦然：全部消費點都在 framework GET path 與 SET path，
ISS GET path 一次都沒引用。

### 7.2 value-file 的處置

| 對象 | 處置 |
|---|---|
| `ies_uri_handle_client` | **保留不動**。19+ 個使用者（`app_moxa_iss_config_loader`、`lib_moxa_rust_iss`、多支 plugin 的 `framework/src/command.rs`、python command script…），大多與 SNMP 無關 |
| value-file 快取 | **退出 GET 主線**。桶 A / 桶 B 的 GET 都走 AgentX |
| in-master fallback | **原樣保留**。subagent 斷線時的降級路徑 |
| framework subagent 內部 | **不重建這套檔案機制**。批次取值（一次拿整張表）是否需要，待 config/status layer 查詢成本量測後決定 |

---

## 8. dlmod 的處置

### 8.1 GET 遷移，SET 留在 `.so`

dlmod plugin 對 framework 的呼叫統計：`/api/v1/setting` 321 次、`/api/v1/status` 162 次、
`/api/v1/command` 3 次，全部經 `lib_moxa_snmp_plugin.c:144` 的
`LibFrameworkUri_CreateHandle("127.0.0.1", 80)` → nginx → config_layer / status_layer。
**與 `iss_build==0` backend 完全相同**，因此比照處理。

3 個 command-layer 呼叫全部只出現在 SET 路徑：

| plugin | URI | 觸發的動作 | mode |
|---|---|---|---|
| `plugin_moxa_locator` | `/api/v1/command/locator` | POST `{"duration":N}` 閃定位燈 | `MODE_SET_ACTION` |
| `plugin_moxa_relay` | `/api/v1/command/relayOverride` | POST `{"id":N}` 繼電器 override | `MODE_SET_ACTION` |
| `plugin_moxa_goose_check` | `/api/v1/command/gooseCheckStreamControl` | POST 啟停 GOOSE stream 監看 | SET/commit |

**GET 側完全不碰 command layer**，不需特殊處理。

dlmod 的載入是執行期決定的：每支 plugin 安裝一個 `.conf` 到 `/etc/moxa/netsnmp/config/`，
`setup_snmpd_plugin_conf()`（`config_moxa_snmp_control.c:393`）以
`find <該目錄> -name '*.conf'` 組出逗號清單餵給 `snmpd -c`。
因此載入集合 = 安裝集合 = `BR2_PACKAGE_PLUGIN_MOXA_*`，隨型號而異（見 §9.2 的產品相依性警告）。

**同一 arc 可能由多支 plugin 分佔**：`8691.603.4.10`（mxLp）同時被 `plugin_moxa_lp`
與 `plugin_moxa_iec62439_2/snmp/status`（`sMxLpStatusOid = 8691.603.4.10.2`）服務。
Phase 7 拆分時這類共用 arc 要一起處理，不能各自為政。

### 8.2 三層障礙

1. **mapping 不在 `net_*.h`**：在各 plugin 的 `LibSnmpPlugin_OidColumn[] / OidScalar[]` 宣告表
2. **不是純資料**：`.mGetData` 可能是 plugin 自訂 C 函式（`_GetStringArrayFirst`、
   `_GetStringArraySecond`、`_GetDummyFalse`…）
3. **硬連結**：`lib_moxa_snmp_plugin.h:54` 宣告
   `extern char security_tokens[MAX_TOKEN_NUM][TOKEN_SIZE]`，`src/` 內無定義，
   實際定義在 `ies_auto_mibs.c:159-160`（snmpd 主體）。這是 `.mk` 需要
   `--with-ldflags="-rdynamic"` 的原因

### 8.3 解法：同一份原始碼編兩次

因為 framework subagent **內嵌 libnetsnmpagent**，`lib_moxa_snmp_plugin` 與 plugin `.c`
使用的 net-snmp 型別（`netsnmp_variable_list`、`snmp_set_var_typed_value`、handler 註冊）
在 framework 進程中**是存在的**，程式碼可幾乎原樣編入。需換掉的只有兩處：

| 現況 | framework 內 |
|---|---|
| `LibFrameworkUri_GetUri()` → nginx → actix | in-process 呼叫 config/status layer |
| `security_tokens[]`（來自 ies-auto-mibs） | GET 不需身分 → 移除相依 |

`LibSnmpPlugin_Oid*[]` 宣告表維持**單一真相來源**，編進 snmpd `.so`（只用
`mSetData` / `mValidateData`）也編進 framework（只用 `mGetData`）。

> 這回頭驗證了「內嵌 libnetsnmpagent 而非純 Rust 自刻 AgentX」的價值：
> 純 Rust 方案下這 30+ 支 plugin 等於全部重寫。

### 8.4 已知取捨

`plugin_moxa_relay` 與 `plugin_moxa_locator` 用
`sUriToken = security_tokens[token_cnt + 1]` 依 `securityName` 挑 token，
**有做 SNMPv3 per-user 歸屬**。由於它們的 SET 留在 snmpd，此歸屬**不受影響**。

---

## 9. ISS 側工作

### 9.1 補三組 wrapper

`iss_build==1` 但 ISS 端無對應 Moxa MIB 的只有 3 組：`mxLadb`、`mx_vlan`、`mx_lldp`
——正好就是唯一帶 `ISS_REMAP_TBL` / `ISS_REMAP_MERGE_TABLE` 旗標的群組
（snmpd 拿 ISS 的 2076 私有樹「合成」出來的 Moxa MIB）。

ISS 內部有對應 nmh：`code/future/la/src/fslawr.c:21`
`SNMPRegisterMibWithLock(&fslaOID, &fslaEntry, LaLock, LaUnLock, ...)`、
`vlangarp/vlan/src/fsvlanwr.c`、`fsmsbewr.c` 等，只是掛在被排除的 2076 下。

工作：仿照現有 22 個 `RegisterMX*` 模組（樣板 `mab/src/mxMabwr.c`，
把 `nmhGetMabConfig*` 包成 Moxa OID 的 MIB entry），新增 `mxLa` / `mxVlan` / `mxLldp`
三組 wrapper 掛在 Moxa arc。

**兩套 OID 的對應關係**（以 `mxLa` 的 `ifMainType` 為例）：

| 端 | OID | 意義 |
|---|---|---|
| snmpd / `net_mxLadb.h` | `1.3.6.1.4.1.8691.603.1.2.1.1.1.2` | Moxa 對外 OID |
| ISS / `fscfadb.h:38` | `1.3.6.1.4.1.2076.27.1.4.1.2` | Future Software 內部 OID |

```c
/* code/future/cfa2/inc/fscfadb.h:38,203 */
UINT4 IfMainType [ ] = {1,3,6,1,4,1,2076,27,1,4,1,2};
{{12,IfMainType}, GetNextIndexIfMainTable, IfMainTypeGet, IfMainTypeSet, IfMainTypeTest,
 IfMainTableDep, SNMP_DATA_TYPE_INTEGER, SNMP_READWRITE, IfMainTableINDEX, 1, 0, 0, NULL},
```

目前外部 GET 的完整路徑：

```
GET 8691.603.1.2.1.1.1.2.<idx>
  → ies-auto-mibs entry：uri = "fscfadb/ifMainTable/", uri_index = "ifMainType"
  → ISS URI thread 依 URI 查表
  → 得到 ISS OID 2076.27.1.4.1.2 + index
  → IfMainTypeGet → nmhGetIfMainType
```

**所以 ISS 內的 GET handler 本來就是 `IfMainTypeGet` / `nmhGetIfMainType`；
缺的只是一筆掛在 Moxa arc 底下、指向同一組 function pointer 的 MIB-DB 列。**
工作是新增 `{{n, MxLaIfMainType}, GetNextIndexIfMainTable, IfMainTypeGet, IfMainTypeSet, …}`
——OID 陣列換掉、function pointer 原樣沿用，與 `mxMabwr.c` 同一種形狀。

**需逐組驗證的一點：port index 空間。**
URI hook 路徑上有 `remapper_handle_table_{read,update}_for_port_index_table`
（`iss_snmp_uri_remap_fscfadb.c`、`iss_snmp_uri_remap_fsladb.c`），
底層 `remapper_util_check_data_port()`（`iss_snmp_uri_remap.c:376`）會依
`gMaxPortInSystem` / `gMaxPortInPlatform` 做**過濾**（`OID_PORT_FILTER`）與
**重編號**（`OID_PORT_UPDATE`），且分 `INCLUDE_MOXA_L3` 條件編譯。
直接註冊 Moxa arc 會走 ISS 的 SNMP 側 index，繞過這層。

實測顯示 ifTable / ifXTable **沒有分歧**（§2.5：col 2 之後逐欄 index 集合相同，
含 `130`），代表 ISS 的 SNMP 側 index 與 remap 後的結果一致。
但**這只由 ifTable 一例證實，mxLa 對到的是 `laConfigGroupTable` /
`laStatusGroupTable` / `laConfigIfMainTable` / `laConfigPortTable` 四張不同形狀的表**
（見 `ies_auto_mibs_handle_iss_remap_table.c`），必須逐張以 walk 比對 index 集合，
不可外推。

### 9.2 ISS 註冊範圍 vs `iss_build==1` 的雙向落差

ISS 端共 **37 個** Moxa private MIB root（掃描 `code/future/**/mx*db.h` 的
`UINT4 <name>[] = {1,3,6,1,4,1,8691,…}`），全部都有 `SNMPRegisterMib` / `SNMPRegisterMibWithLock`。
`iss_build==1` 落在 8691 私有樹的有 323 筆。兩者**不是等價集合**。

#### 方向一：ies-auto-mibs 有、ISS 未覆蓋 —— 23 筆

| 群組 | 筆數 | OID arc |
|---|---|---|
| `net_mxLadb.h` | 11 | `8691.603.1.2.1` / `.2` |
| `net_mx_lldp.h` | 6 | `8691.603.5.1.1` |
| `net_mx_vlan.h` | 6 | `8691.603.2.3.1` |

即 §9.1 要補的三組。

#### 方向二：ISS 有註冊、`iss_build==1` 不管 —— 15 個 root

| ISS root | 名稱 | 實際服務者 |
|---|---|---|
| `8691.602.1.10` | mxEip | `plugin_moxa_eip`（dlmod） |
| `8691.602.1.11` | mxProfinet | `plugin_moxa_profinet` |
| `8691.602.2.3` | mxDhcpRelay | `plugin_moxa_dhcp_relay` |
| `8691.602.3.4` | mx1588 | `plugin_moxa_ptp` |
| `8691.603.3.8` | mxMcp | `plugin_moxa_multicoupling` |
| `8691.603.4.10` | mxLp | `plugin_moxa_lp` **＋** `plugin_moxa_iec62439_2/snmp/status`（`sMxLpStatusOid = 8691.603.4.10.2`）—— 同一 arc 由兩支 plugin 分佔不同子節點 |
| `8691.603.4.12` | mxIpsg | `plugin_moxa_ipsg` |
| `8691.603.4.13` | mxDai | `plugin_moxa_dai` |
| `8691.603.5.8` | mxGc | `plugin_moxa_goose_check` |
| `8691.605.4.1` | mxMR | `plugin_moxa_multicast_routing` |
| `8691.603.3.9` | mxPhr | **本型號未啟用**（`# BR2_PACKAGE_PLUGIN_MOXA_PHR is not set`；`mxPhr.mib` 存在但不在本型號 profile） |
| `8691.603.3.10` | mxSup | **本型號未啟用**（`# BR2_PACKAGE_PLUGIN_MOXA_SUPERVISION is not set`；`mxSupervision.mib`（MOXA-PRP-HSR-MIB）存在但不在 profile） |
| `8691.603.3.12` | mxMrp | **由 ISS 提供**：profile `mxMrp=YES`、`mxMrp.mib` 存在，ies-auto-mibs 無 entry、無 dlmod plugin 註冊 → 唯一提供者是 ISS 的 AgentX 註冊。**mainline 無 AgentX master，此 MIB 實際無人服務；Plan E 是它能被曝露的前提**（基線未涵蓋此 arc，待重建後確認） |
| `8691.603.4.7` | mxAcl | **ISS 有完整實作，Moxa 未宣告**：`code/future/ISS/{bcm,xcat,xcat_v11}/inc/mxAcldb.h` 有 `mxAclOID = {10, mxAcl}` 與 58–62 個 OID（`AclConfigMacListIndex`、`AclConfigSrcMacAddr`… 各帶 Get/Set handler）。但 `snmp_moxa_mib` 無 ACL `.mib`、profile 無 mxAcl。`BR2_PACKAGE_PLUGIN_MOXA_ACL=y` 但該 plugin 不做 SNMP → 見 §2.6(c) |
| `8691.603.4.15` | mxVa | **ISS 有完整實作，Moxa 未宣告**：`code/future/va/inc/mxVadb.h:13-30` 有 `mxVaOID = {10, mxVa}` 與 3 個物件（`VaConfigGlobalEnable`、`VaConfigPortIndex`、`VaConfigPortEnable`，帶 Get/Set handler）。Moxa 側無 `.mib`、profile 亦無 → 見 §2.6(c) |

判定依據（三個來源交叉）：

1. **`snmp_moxa_mib/product/<型號>.profile`** —— 官方「本型號應曝露哪些 MIB」清單，權威來源
2. **`snmp_moxa_mib/private/*.mib`** —— Moxa 是否對外定義過此 MIB
3. **`buildroot/.config` 的 `BR2_PACKAGE_PLUGIN_MOXA_*`** —— 本型號是否安裝該 plugin

MIB 定義與 ISS OID 的對照（`mxSwitching.mib:46`：`layer2Redundancy ::= { switching 3 }`，
`switching = 8691.603`，故 `layer2Redundancy = 8691.603.3`）：

| `.mib` | 定義 | 解析後 OID | ISS 端 header | 相符 |
|---|---|---|---|---|
| `mxMrp.mib:33` | `::= { layer2Redundancy 12 }` | `8691.603.3.12` | `mxMrpdb.h` | ✓ |
| `mxPhr.mib:33` | `::= { layer2Redundancy 9 }` | `8691.603.3.9` | `mxPhrdb.h` | ✓ |
| `mxSupervision.mib:32` | `::= { layer2Redundancy 10 }` | `8691.603.3.10` | `mxSupdb.h` | ✓ |

三者的 Moxa MIB 定義與 ISS 註冊 OID 完全吻合，證明它們是正規的 Moxa 私有 MIB、由 ISS 供應。

> **不可用「`dl/` 底下有無該目錄」判定 plugin 是否存在**：`dl/` 只含本組態實際 fetch 的套件，
> 目錄不存在只代表未啟用。這是本文件初稿的判定錯誤，已修正。
> 另：`plugin_moxa_*/python/` 是架構轉 Rust 前的死碼，不可作為 plugin 能力的佐證。

> **這張表是產品相依的。** dlmod 的載入清單不是編譯期靜態決定，而是
> `setup_snmpd_plugin_conf()`（`config_moxa_snmp_control.c:393`）在執行期以
> `find /etc/moxa/netsnmp/config/ -name '*.conf'` 動態組出，餵給 `snmpd -c <list>`。
> 因此「誰服務這個 root」= 「哪些 plugin 實際被安裝」= `BR2_PACKAGE_PLUGIN_MOXA_*`。
>
> 本表依 MDS-G4000-L3-4XGS 的 `buildroot/.config` 判定，前 10 項對應的
> `BR2_PACKAGE_PLUGIN_MOXA_{EIP,PROFINET,DHCP_RELAY,PTP,MULTICOUPLING,LP,IPSG,DAI,GOOSE_CHECK,MULTICAST_ROUTING}`
> 全部為 `=y`。**換型號（EDS / TN / MRX）若某些 plugin 未開啟，對應的 ISS root
> 就從「被 dlmod 蓋住」變成「無主」，OID 將直接由 ISS 回答。**
> 每個要出貨的型號都必須重跑這份比對。

#### 兩個實質影響

**(a) 前 10 項是 §4.4 不變式的現成案例，Phase 7 會改變保護方式。**
目前 dlmod plugin 註冊 leaf 級（`OidScalar` / `OidColumn`，namelen 長），ISS 註冊 root
（namelen 短），因此 dlmod 勝出 —— **這正是私有樹 walk diff 為 0/0 的原因**。
Phase 7 把 dlmod 的 GET 搬進 framework subagent 後，children 鏈變成三層：

```
1. in-master（必須有等長註冊，依 §4.4）    ← head
2. framework subagent（leaf 級，namelen 長） ← 轉發取到這個，語意正確
3. ISS（root 級，namelen 短）
```

結果正確，但**保護機制從「dlmod 在本地」換成「namelen 排序」**。
Phase 7 必須明確驗證這 10 個 root，不得假設。

**(b) 後 5 項是潛在的 OID 洩漏。**
ISS 有註冊、本地無人蓋。私有樹 diff = 0 表示目前不回值（ISS 那些模組在本產品可能未啟用，
或 nmh 回 `NO_SUCH`），但這是靠 **ISS 執行期行為**而非結構保證。
`mxPhr` 之類對應 `BR2_MOXA_PHR` 編譯旗標——換產品型號或啟用該功能，
就可能冒出未經驗證的 Moxa private OID。列入 §12.1 待決。

### 9.3 收斂多餘註冊

`SnxMainRegisterMibs()`（`snxmain.c:870`）以 **MIB root 粒度**註冊，
底下不再細分，因此帶出 §2.5 那 483 筆未實作節點。
已排除 `enterprises.2076`；其餘需在 ISS 端不註冊（優先）或以 VACM `view all excluded` 擋掉
（`config_moxa_snmp_control.c:859-883` 已有部分）。

### 9.4 註冊粒度與 priority

- ies-auto-mibs：現行逐欄位註冊 → 改整段
- ISS：`SnxMainRegisterMibs()` 只註冊每棵 MIB 的 root

master 靠「namelen 長者勝」仲裁，**不是靠 priority**。這正是 Plan C
「不註冊就自動落到 ISS」能運作的原理。整段註冊後兩邊 namelen 相同，
才需要 priority 錯開（§4.2）。**不建議把 priority 當作路由手段**——
會讓仲裁從確定性的 OID 長度規則，變成長度 + priority + 註冊順序的混合規則。

---

## 10. 分階段實作與驗收

### 10.1 驗收框架（每個 phase 共用）

對照組 = 同型號同設定的 NOS mainline；實驗組 = Plan E build。
各跑 `1.3.6.1.2` 與 `1.3.6.1.4` 全樹 snmpwalk，比對 OID 集合與逐筆值，
產物存入 `~/WORK/SNMP_50ms/snmpwalk/` 並納入版控。

### 10.1.1 前置：重建基線（Phase 0 的第一件事）

§2.5 的警告指出既有基線並未涵蓋 `8691.603.*`，且兩份 603 walk 都提早中止。
在任何 phase 開始前必須先處理：

1. **修掉 602/603 交界的 endOfMibView**（§2.5(a)、§12.3）。這是 mainline 既有缺陷：
   從 `1.3.6.1.4` 起跑的 walk 在 `8691.602.5.1.1.8.0` 之後就宣告結束，
   使 `8691.603.*` 完全無法透過全樹 walk 取得。**不修就取不到有效基線，也無法定義驗收。**
   （603 walk 檔的中止是人為停止，非缺陷，重測即可。）
2. **重取完整基線**，mainline 與 Plan E build 各一份，至少涵蓋
   `1.3.6.1.2`、`1.3.6.1.4.1.8691.602`、`1.3.6.1.4.1.8691.603`、`1.3.6.1.4.1.8691.605`、
   `1.0.8802`、`1.3.111`、`1.2.840`。
3. **walk 完整性自檢**：比對每次 walk 的最後一筆 OID 與預期上界；提早中止即視為
   該次驗收無效，不可當作「差異為零」。
4. **以 `snmp_moxa_mib/product/<型號>.profile` 為應曝露清單**，比對實際 walk 結果，
   同時抓出「該有卻沒有」與「不該有卻出現」兩個方向。

| 條件 | 定義 | 何時必須成立 |
|---|---|---|
| **無回歸（硬門檻）** | `mainline − planE = ∅`，且共同 OID 的型別與值逐筆相同 | **每一個 phase** |
| **延遲** | 該 phase 涵蓋的熱路徑 OID，GET p100 < 50 ms | 每一個 phase |
| **溢出已分類（收尾門檻）** | `planE − mainline` 的每一筆都已判定為「合法補回」或「洩漏」，且洩漏已關閉 | 最終 phase |

> **溢出門檻不是「歸零」。** 初稿寫成 `planE − mainline = ∅` 是錯的：
> 那 483 筆中至少 26 筆是補回 mainline 缺失的 `ifIndex` / `ifName`（§2.5），
> 照「歸零」執行會把 IF-MIB 的必要物件再弄壞一次。
>
> 正確做法是逐筆分類：
>
> - **(a) 合法補回** —— 在 `snmp_moxa_mib/product/<型號>.profile` 內，或屬 RFC 規定的
>   必要物件（如 `ifIndex`、`ifName`）→ **保留**，並更新對照基線
> - **(b) 已實作但未宣告** —— ISS 依 RFC 或依 Moxa 私有 arc 正確實作，
>   但 Moxa 未 ship 對應 `.mib`、未列入 profile（如 RSTP-MIB 的 dot1dStp 延伸、
>   IP-MIB、`mxAcl`、`mxVa`）→ **產品決策**：關閉，或補 `.mib` 並加入 profile
>
> 分類結果須逐筆記錄，未分類的溢出視同未通過。
> **(b) 類的處置屬 RD / PM 職權，不是實作者可自行決定的技術細節。**
> 483 筆的完整分類見 §2.6。

任一 phase 硬門檻不過即 rollback，不往下走。

**平台**：本階段僅驗證 **Cortex-A9**；CN9130 不納入。

### 10.2 Phase 分解

策略是「**試點 → benchmark 決策點 → 全量**」，不是逐步推進。

| Phase | 內容 | go/no-go |
|---|---|---|
| **0** 基礎設施 | **先做 §10.1.1 重建基線**（查明 walk 中止原因、取完整 mainline/Plan E 基線）；`.mk` 加 `agentx/subagent`；建 `lib_moxa_ies_auto_mibs` + Rust FFI；建 `lib_moxa_snmp_agentx` + Rust FFI | 完整基線取得且 walk 未提早中止；Cortex-A9 交叉編譯過；差異不得較基線惡化 |
| **1** 機制一 + 機制二 | ies-auto-mibs 改整段註冊（priority 100）；實作 `->children` 轉發；掛 `SNMPD_CALLBACK_REGISTER_OID` 監聽器檢查 §4.4 的 namelen 不變式；刪 `agentx_owned.list` 與 probe 表。**以 ifTable / ifXTable 驗證**（Plan C 的等價替換，有現成基線可比） | 硬門檻 + 延遲；walk 與 mainline 交叉比對一致；開機 log 無不變式違反 |
| **2** iss1 私有 MIB 試點 | **`mxPortdb`**（17 筆，RO 9 / RW 8，OID 根 `1.3.6.1.4.1.8691.603.1.1`）。ISS 側有 `RegisterMXPORT` + `mxPortdb.h`；5 個 URI 涵蓋 1 scalar + 4 table，其中 `portConfigTable` 是 §3.3 的 24 張風險表之一 → 直接驗證機制一確實解掉合成 index 問題 | 硬門檻；`portConfigTable` 的 col-1 由 ISS 提供且值正確 |
| **3** framework subagent 骨架 + iss0 試點 | 內嵌 libnetsnmpagent、fd 掛 tokio。試點兩組：**`mx_device_io`**（8 筆、全 RO、走 status layer、實際輪詢對象）取 benchmark 數據；**`mx_portmirror`**（18 筆、RO 2 / RW 16、含 4 個 `REPLACE_TRUTH_VALUE`）驗證 §7 語意轉換移植 | 硬門檻；SET 仍走 in-master 不變 |
| **4** **Benchmark 決策點** | 對 Phase 2（iss1）與 Phase 3（iss0）試點做導入前 / 後延遲對比 | **全量遷移的 go/no-go**。效益不成立就停在此處重新評估 |
| **5** 全量 `iss_build==1` | 逐 MIB 上線；含 §9.1 ISS 補 mxLa / mxVlan / mxLldp wrapper | 每組硬門檻 |
| **6** 全量 `iss_build==0` | 移植 §7 全部語意轉換；分組上線，567 筆事件組態表放最後 | 每組硬門檻 |
| **7** dlmod GET 遷移 | `lib_moxa_snmp_plugin` backend 抽象化；plugin 原始碼編兩次；拆 `security_tokens[]` 相依 | 硬門檻；SET 行為不變；fiber_check 延遲改善量測；**§9.2(a) 的 10 個 root 逐一驗證 children 鏈順序**（保護機制由「dlmod 在本地」換成 namelen 排序） |
| **8** 收尾 | 關閉 483 筆溢出；移除 snmpEngine 4 筆；sysObjectID 收進 Path 1 | 雙向差異為零 |

### 10.3 為什麼 Phase 1-2 排在 framework 之前

Phase 1 完成後，撐不住規模的兩個機制（`agentx_owned.list`、probe-OID）已被換掉；
Phase 5 完成後 ISS 側 1391 筆成果落地。framework 那半邊（Phase 3、6、7）是全新程式碼、
風險高得多，即使延後或縮減，Plan E 的主要效益已經拿到。
**Phase 1+2+5 構成一個可獨立出貨的里程碑。**

---

## 11. 決策日誌

| # | 決策 | 理由 | 被否決的方案與原因 |
|---|---|---|---|
| D1 | `agentx_owned.list` 廢除，改為整段註冊 + 無條件 GET 轉發 | 清單 key（URI 前綴）與路由空間（OID）不對應；`mxLadb` 反例會產生 `noSuchObject`。改成結構性路由後無清單可維護 | (a) 改用 OID 前綴清單：仍需維護且會與 ISS 實際註冊不同步。(b) 保留 URI 清單加 OID 驗證閂：註冊時機早於 AgentX 連線，驗證拿不到結果 |
| D2 | GET 一律出 snmpd（不分 RO/RW）；SET 一律 in-master | `iss_build==0` 只有 9% 是 RO，「只 offload RO」規則在 framework 側失效 | 桶 B 只收 RO：涵蓋率過低，無實質效益 |
| D3 | 逐欄位註冊改整段註冊（FRR `register_mib` 風格） | 順帶消滅 §3.3 的 24 張表合成 index 風險，以及 §4.3 的 GETNEXT 邊界風險 | 維持逐欄位並個別修 `GenerateTableIndexEntry`：治標，且 GETNEXT 風險仍在 |
| D4 | 機制二走 `netsnmp_subtree` 的 `->children` 鏈 | probe-OID 依賴「同區內存在 ISS 獨佔 RO instance」，是每張表的偶然性質，無法通用化。children 走訪是 net-snmp 自己的慣用法（`agent_registry.c:1705`） | (a) 自建 `SNMPD_CALLBACK_REGISTER_OID` 路由表：耦合較黏但多一份狀態要跟 subagent 斷線/重連同步。(b) 沿用 probe-OID：無法規模化 |
| D4a | 接受伸手進 net-snmp 內部結構的耦合 | `netsnmp_subtree` 的 children/namelen/priority 佈局在 5.x 穩定；依賴點集中在單一函式約 15 行 | — |
| D4b | 多 subagent 的正確性靠 **namelen 不變式**（§4.4）維持，並以 `SNMPD_CALLBACK_REGISTER_OID` 監聽器偵測違反 | 排序鍵 namelen 優先於 priority，因此 priority 無法保護 in-master：任何註冊粒度比 in-master 細的 subagent 會直接搶下 head，使 SET 與 GET 轉發雙雙失效 | (a) 靠文件約定要求 subagent 只註冊寬 root：無強制力，違反時症狀隱晦。(b) in-master 退回逐欄位註冊以確保 namelen 最長：失去機制一的全部好處（合成 index、GETNEXT 邊界又回來） |
| D5 | framework subagent 內嵌 `libnetsnmpagent`（FRR 風格） | RFC 2741 編解碼、GETNEXT/GETBULK、table_iterator、AgentX 重連全部由 net-snmp 代勞，行為與 snmpd 端一致。並使 dlmod plugin 可幾乎原樣編入（§8.3） | (a) 純 Rust 自刻 AgentX：需重實作 GETNEXT/index 排序語意，且 dlmod 30+ 支 plugin 等於全部重寫。(b) 不做 framework subagent 只強化 dlmod：放棄 unified 目標，瓶頸只搬家 |
| D6 | 新增 `lib_moxa_snmp_agentx`（C）+ `lib_moxa_rust_snmp_agentx`（Rust）兩層 | 主要是**封裝邊界**：`libnetsnmpmibs` 內含 ies-auto-mibs，直接連 `--agent-libs` 會把它拖進 framework。次要是把 net-snmp 的 union / 巨集收斂成純量 API 再 FFI | 直接在 framework 用 bindgen 對 `libnetsnmpagent`：會連帶拖入 `libnetsnmpmibs`，且 bindgen 產出脆弱 |
| D7 | GET 路徑不持有任何 token；SET 路徑 token 機制完全不動 | AgentX PDU 不帶 `securityName`，subagent 在協定上拿不到身分。master 已完成 USM + VACM。身分複雜度全部關在未改動的 SET 路徑 | (a) 固定內部服務身分 token（role admin）：GET 不需要，多餘權限。(b) 沿用 `security_token`：同上，且無意義 |
| D8 | dlmod：GET 遷入 framework，SET 留在 `.so`；同一份原始碼編兩次 | 宣告表維持單一真相來源 | (a) SET 也搬進 framework subagent：**明確否決**（管理層不接受）。(b) 補進 ies-auto-mibs（新增 `net_*.h` + in-master SET handler）：需重寫 ~370 條驗證邏輯，工作量過大 → 列入 future works |
| D9 | `ucd-snmp/dlmod` 模組本身保留 | dlmod 是 net-snmp 通用外掛載入機制，與「Moxa 用它承載 OID」是兩件事；保留可供將來臨時掛載 | — |
| D10 | `ies_uri_handle_client` 保留不動 | 19+ 個使用者，大多與 SNMP 無關 | — |
| D11 | `mxLa` / `mxVlan` / `mxLldp` 在 ISS 端補 AgentX wrapper | ISS 內部已有對應 nmh（`fslawr.c`、`fsvlanwr.c`），只是掛在被排除的 2076 下 | 留在 in-master：這三組會成為唯一的例外，破壞規則一致性 |
| D12 | `snmpEngine`（`1.3.6.1.6.3.10.2.1.{1,2,3,4}`）4 筆移除 | 與 net-snmp 原生 `snmpv3/snmpEngine.c` 重疊（`default_modules.h:6` 對 mini-agent 也生效）。實機驗證 `snmpEngineTime = 0` 為錯值，佐證正繞道 framework REST | 收進 Path 1：仍是重複實作 |
| D13 | 本階段僅驗證 Cortex-A9，CN9130 不納入 | 避免節外生枝；目前實作以 Cortex-A9 為主 | — |
| D14 | Phase 排序為「試點 → benchmark 決策點 → 全量」 | 效益未經量測前不做全量遷移 | 逐步推進：全量成本在效益未驗證前就已投入 |

---

## 12. 待決事項與 Future Works

### 12.1 待實測決定

| 項目 | 說明 |
|---|---|
| framework subagent 的批次取值 | value-file 的「一次取整張表」語意是否需要在 in-process 重現。取決於 config/status layer 單次查詢成本 |
| 483 筆溢出的關閉方式 | ISS 端不註冊（優先）vs VACM exclude（備案），逐段評估 |
| `mxArpdb` 的 `URI_FIXED_VALUE` | `ies_auto_mibs_setup_net_mxArpdb.c` 對 `iss_build==1` 的 entry 設了 `URI_FIXED_VALUE`，但該旗標只在 `entry_handle_generate_uri_value_file()`（framework path，`:2425`）被檢查，ISS path 不看。疑似 dead code 或路由不符預期，Phase 5 遷移該組前須確認 |
| `mxMrp` 的服務者（§9.2） | profile 為 `mxMrp=YES`、`mxMrp.mib` 存在，但找不到註冊 `8691.603.3.12` 的程式。須確認是 ISS 直接提供、尚未實作、或由他處承載。基線未涵蓋此 arc，無法從 walk 判定 |
| **101 筆「已實作但未宣告」的處置**（§2.6(b)） | RSTP-MIB 的 `dot1dStp` 延伸 74 筆、IP-FORWARD-MIB 13 筆、IP-MIB 14 筆。ISS 依 RFC 實作正確，Moxa 未 ship 該 MIB 模組。**須 RD / PM 決定**：關閉，或補 `.mib` 並加入 product profile |
| `mxAcl` / `mxVa` 的處置（§2.6(c)、§9.2） | ISS 有完整 MIB-DB 與 Get/Set handler（mxAcl 58–62 個 OID、mxVa 3 個），Moxa 未 ship `.mib`、profile 亦無。同為產品決策。基線未涵蓋此 arc，須先取得完整 walk 確認實際回值 |
| `mxPhr` / `mxSup` 於其他型號 | 本型號未啟用故無衝突。若某型號啟用了 plugin，需確認 dlmod 與 ISS 兩邊誰服務、是否符合 §4.4 不變式 |
| 603 walk 提早中止的原因 | mainline 停在 `8691.603.3.2.2.1.1.16.12`（mxRSTP 中途）。屬既有問題，但會阻擋基線重建，須於 Phase 0 排除 |
| 逐型號重跑 §9.2 比對 | §9.2 的歸屬表依 MDS-G4000-L3-4XGS 組態判定。dlmod 載入清單由 `BR2_PACKAGE_PLUGIN_MOXA_*` 決定，換型號可能使某些 ISS root 失去本地覆蓋。每個出貨型號都要重跑一次比對並記錄結果 |

### 12.2 Future Works

| 項目 | 說明 |
|---|---|
| config / status layer 查詢成本優化 | 獨立 action item，待 Plan E 架構完成後進行 |
| dlmod OID 補進 ies-auto-mibs | D8 的方案 (b)。完成後 dlmod 可完全消失，SET 回歸單一框架 |
| PDF Tier 1 / Tier 2 優化 | 編譯 flag `-O0` → `-O3 -flto`、Rust `opt-level` 3；退掉 4.2 秒 sleep；`rename()` 取代 `LibUtil_ExecShCmd("/bin/mv")`；shared library function call 取代 fork+exec。與 AgentX 互補，可獨立疊加 |
| 新增 SNMP OID subtree 的標準步驟 | Plan E 完成後整理 |

### 12.3 已知殘留問題（非本次範圍）

- VRRPv2（`.68`）/ VRRPv3（`.207`）回 `badValue` 導致 snmpwalk 中斷，
  目前以 VACM exclude 迴避（`config_moxa_snmp_control.c:882-883`）
- `snmpEngineTime` 回傳 0（由 D12 一併解決）
- **mainline 的 `ifTable` 缺 `ifIndex`、`ifXTable` 缺 `ifName`**（IF-MIB 必要物件）。
  推測為 `MAKEUP_INDEX_FOR_ISS` 對 ifTable 未生效。**Plan C 已順帶修好**（§2.5），
  Plan E 沿用同一機制，故不需額外處理——但驗收時不可把它當成「溢出」而關掉
- **`8691.602` → `8691.603` 交界回 endOfMibView**：從 `1.3.6.1.4` 或 `.1` 起跑的
  全樹 snmpwalk 在 `8691.602.5.1.1.8.0` 之後即宣告 `End of MIB`，
  導致整個 `8691.603.*`（ISS 供應的 Moxa 私有 MIB，本型號 profile 中有數十個
  `=YES` 項目）**無法經由正常 walk 取得**；必須以 `8691.603` 為根另跑才看得到。
  mainline 與 Plan C 行為一致，為既有缺陷。
  影響超出 Plan E 範圍（NMS 全樹輪詢會漏資料），但**阻擋基線建立，故列為 Phase 0 前置**
