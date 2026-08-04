# Plan E 設計定案與 Phase 0/1 實作

- 日期：2026-08-03 ~ 08-04
- 產出：設計文件、實作計畫、Task 1–5 程式碼、真機驗證與一個回歸修正

---

## TL;DR

把 SNMP GET 從 snmpd 卸載到 AgentX subagent，用「整段註冊 + `->children` 鏈轉發」取代 Plan C 撐不住規模的兩個機制；真機實測延遲從 630 ms 降到 0.00 s，並靠全子樹 diff 抓出一個靜態檢查看不到的回歸。

---

## 背景與目標

- 每個 SNMP GET 要在 **50 ms** 內完成，與 eCos 時代的 criteria 對齊
- GET 路徑要收斂成單一機制，消滅「config 走 framework / status 走 ISS / private 走 dlmod」三套並存
- SET 的持久化模型不能動，framework JSON 維持 source of truth
- Plan C 已有可運作的原型，但只涵蓋 ifTable/ifXTable，需要判斷能否擴大
- Plan D 的架構被判定有問題，本次不參考

本次從「確認 codebase 狀態」開始，先釐清 Plan C 擴大會遇到什麼坑，再產出完整的 Plan E 設計文件與可執行的實作計畫，最後實際跑完 Phase 0 與 Phase 1。目標不只是效能，過程中也發現這件事同時牽涉 MIB 一致性。

---

## 參考資料 / 來源

- `~/WORK/SNMP_50ms/SNMP Enhancement 2026_*.pdf` — Confluence 匯出的專案總覽，含 Plan C/D/E 定位與 TODO
- `~/WORK/SNMP_50ms/snmpwalk/` — 既有的 snmpwalk 基線（NOS7 對照組、Plan C、Plan D）
- `dl/3rdparty_net_snmp/ies-auto-mibs/` — 2136 個 OID entry 的定義與 handler
- `dl/app_moxa_iss_10_1_0/code/future/` — ISS 的 MIB 實作與 AgentX subagent
- `dl/snmp_moxa_mib/` — 官方 MIB 定義與**逐型號的 `product/*.profile`**
- `net-snmp-5.9.3/agent/agent_registry.c` — subtree 註冊仲裁邏輯（本次設計的地基）
- `~/SRC/frr/lib/agentx.c` — FRRouting 的 AgentX 整合，作為 framework subagent 的樣板

其中 `product/*.profile` 是這次才被納入的權威來源——它定義「本型號應該曝露哪些 MIB」，是判斷 OID 該不該存在的唯一依據。

---

## 討論脈絡 1：Plan C 的機制能不能擴大

- **問題**：把 `agentx_owned.list` 套用到其他 `iss_build==1` 節點，會不會遇到 mod_uri / rowStatus / createAndGo 問題
- **查證結果**：`mod_uri` 只在 framework GET 與 SET 路徑被消費，ISS GET 路徑完全不引用 → **non-issue**
- **查證結果**：`READ_CREATE_*` 全部在 SET 路徑，而清單規則第一行就排除 RW → rowStatus **機制上不會壞**
- **但發現三個沒被問到的問題**：清單以 URI 為 key 但路由發生在 OID 空間、合成 index 會影子覆蓋 24 張表、`iss_build==0` 只有 9% 是 RO
- **決定**：兩個既有機制都撐不住，全部換掉

原本的提問方向是對的，但答案是「你擔心的兩件事不會壞，真正會壞的是別的三件」。`mxLadb` 是最好的反例——它的 OID 在 Moxa 私有樹，URI 卻是 ISS 的 `fsladb/`，而 ISS 把那棵樹排除在 AgentX 之外，照清單做只會得到 `noSuchObject`。

---

## 討論脈絡 2：轉發機制怎麼通用化

- **問題**：probe-OID 轉發只有 4 條硬編碼規則，能不能大型化
- **候選 A**：繼續手工挑 probe OID → 依賴「同區內存在 ISS 獨佔的 RO instance」，是每張表的偶然性質
- **候選 B**：自建 `SNMPD_CALLBACK_REGISTER_OID` 路由表 → 多一份狀態要跟斷線/重連同步
- **候選 C**：走 `netsnmp_subtree` 的 `->children` 鏈 → net-snmp 保留落敗的註冊，且 `unregister_mib_context()` 自己就是這樣走的
- **決定**：採 C，約 15 行，對 ISS 與 framework 兩個 subagent 一體適用
- **代價**：伸手進 net-snmp 內部結構，明確記入決策日誌

關鍵發現是 `netsnmp_subtree_load()` 的排序鍵是 (namelen 由長到短, priority 由小到大)，而落敗的註冊掛在 `->children` 上不會被丟棄。這讓「本地保留註冊承接 SET、GET 轉給 subagent」變成結構性的路由，不再需要任何清單。

---

## 討論脈絡 3：priority 保護不了 in-master

- **問題**：未來有第三個 daemon 註冊 AgentX subagent，priority 要注意什麼
- **推導**：排序鍵 namelen 先比，priority 只在 namelen 相同時起作用
- **後果**：任何 subagent 只要註冊得比 in-master 細，就直接搶下鏈頭
- **症狀**：in-master 的 handler 完全不被呼叫 → **SET 靜默失效、無任何錯誤訊息**
- **決定**：真正的不變式是「in-master 的 namelen 必須 ≥ 任何 subagent」，與 priority 無關
- **對策**：掛 `SNMPD_CALLBACK_REGISTER_OID` 監聽器，違反時寫進開機 log

這是使用者提問才逼出來的結論。原本文件把 priority 寫得像是路由手段之一，那是錯的——把 priority 調到 0 也救不回來。這種缺陷如果留到 Phase 5 才爆，症狀會是「某些 OID 的 SET 突然不生效」，極難查。

---

## 討論脈絡 4：dlmod 的歸屬

- **問題**：dlmod 必須收掉，但它的 OID 完全不在 `ies-auto-mibs` 裡
- **實際規模**：~146 個 OID leaf、117 個 setter、~370 條驗證邏輯，橫跨 30+ 支 `.c`
- **候選 A**：SET 也搬進 framework subagent → **明確否決**（管理層不接受）
- **候選 B**：`.so` 保留只做 SET，GET 轉發 → 與 ies-auto-mibs 用同一機制
- **候選 C**：補進 ies-auto-mibs，dlmod 完全消失 → 要重寫 370 條驗證邏輯
- **決定**：採 B，C 列入 future works

選 B 之後架構反而更一致：in-master 側變成「ies-auto-mibs 與 dlmod `.so` 都只做 SET，GET 一律轉發」，dlmod 不再是「獨立於其他 path 之外」的第四條路。代價是同一份宣告表要在兩個進程都存在，作法是同一份原始碼編兩次。

---

## 討論脈絡 5：靜態盤點的三次翻車

- **第一次**：用「`dl/` 底下有無目錄」判斷 plugin 存在 → 錯，`dl/` 只含本組態實際 fetch 的套件
- **第二次**：把 `dot1dStp.16/17/19` 判為「Aricent 私有延伸」→ 錯，是 RFC 4318 RSTP-MIB 的標準物件
- **第三次**：把 `mxAcl`/`mxVa` 判為「無主洩漏」→ 錯，ISS 有完整 MIB-DB 與 Get/Set handler
- **共同錯因**：由「找不到 Moxa 側服務者」推論為「不該存在」
- **修正後的判定依據**：`product/*.profile` + `private/*.mib` + `BR2_PACKAGE_PLUGIN_MOXA_*` 三者交叉
- **文件已加入通則警語**：ISS 常有完整實作，缺的只是 Moxa 的 `.mib` 與 profile 宣告

這三次都是使用者指正才發現的。教訓是：這個 codebase 裡「某個東西看起來不存在」通常代表我查錯地方，而不是它真的不存在。483 筆溢出 OID 因此從「大多是垃圾」重新分類為「382 筆是 profile 已宣告、mainline 卻沒提供的物件」。

---

## 討論脈絡 6：驗收條件本身是錯的

- **原本寫法**：無溢出 = `planE − mainline = ∅`
- **實測發現**：mainline 的 `ifTable` 缺 `ifIndex`、`ifXTable` 缺 `ifName`，Plan C 把它們補回來了
- **後果**：照「歸零」執行，會把 IF-MIB 的必要物件再弄壞一次
- **改為**：逐筆分類——「合法補回」保留、「已實作但未宣告」交 RD/PM 決定
- **後續又發現**：七月的 mainline 基線不可信，今天的 NOS 7.0 其實**有** ifIndex
- **結論**：跨時間的基線不能當對照組，必須同日重取

這一段的價值在於它示範了驗收條件也需要被驗證。第一版條件如果照做，會把一個修好的東西再弄壞；而支撐該條件的資料本身又是過期的。最後的做法是同日重取 mainline 基線，並把分類邏輯寫進工具。

---

## 討論脈絡 7：真機驗證抓到的回歸

- **現象**：`8691.603` 子樹 diff 顯示 mainline 有 13 筆而 Plan E 缺
- **缺的是**：`8691.603.3.2.2.1.1.1.{1..12}` = `rstpStatPortTable` 的 col 1
- **根因**：`net_mxRSTPdb.h` 只定義 col 2–16，col 1 是 `GenerateTableIndexEntry()` 在本地**合成**的
- **後果**：合成欄位轉發給 ISS，ISS 沒有這一欄 → `noSuchInstance` → OID 從 walk 消失
- **影響範圍**：55 張表有這個形狀，其中 51 張是 `iss_build==1`
- **第一版修法被 subagent 擋下**：`MAKEUP_INDEX_FOR_ISS` 設在 col-2 判斷式之外，51 張表的每個欄位都帶它

這是本次最重要的產出。所有靜態檢查、code review、交叉編譯都通過了，問題只有真機全子樹 diff 才看得到。而且它差點被漏掉——當天只暴露 51 張表裡的 1 張，因為 walk 在 `603.3.2.2.1.1.16.12` 就中止；如果只測 ifTable，這個問題會一路帶到 Phase 5 全量遷移才爆。

---

## 結論與決策

- **D1 `agentx_owned.list` 廢除** — 清單 key 與路由空間不對應，改成結構性路由後無清單可維護
- **D2 GET 一律出 snmpd、SET 一律 in-master** — `iss_build==0` 只有 9% 是 RO，「只 offload RO」在 framework 側失效
- **D3 改整段註冊** — 順帶消滅合成 index 與 GETNEXT 邊界兩個風險（Phase 1 實測後延後至 Phase 3）
- **D4 走 `->children` 鏈轉發** — probe-OID 依賴每張表的偶然性質，無法規模化
- **D4b namelen 不變式 + callback 偵測** — priority 無法保護 in-master，違反時 SET 靜默失效
- **D5 framework subagent 內嵌 `libnetsnmpagent`** — 純 Rust 方案下 30+ 支 dlmod plugin 等於全部重寫
- **D7 GET 路徑不持有任何 token** — AgentX PDU 不帶 securityName，身分複雜度全關在未改動的 SET 路徑
- **D8 dlmod GET 遷入 framework、SET 留 `.so`** — SET 搬進 framework 已被明確否決
- **D15 不重現 value-file 批次取值、絕不用 `ies_uri_handle_client`** — 那正是要消滅的成本來源
- **D16 不修 `602→603` 的 endOfMibView 缺陷** — 屬 mainline 範圍，基線改以分段 scoped walk 取得

決策日誌採不重用編號的規則，被推翻時新增一列註明「取代 Dn」。各章節開頭以 `> 決策依據：Dn` 反向標注，讓實作細節可以追回理由與被否決的方案。

---

## 實測結果

- **延遲**：Plan E p100 = **0.00 s**，mainline p100 = **0.63 s**（20 次 `ifAdminStatus` GET）
- **SET 路徑**：`1 → SET 2 → 讀回 2 → SET 1 → 讀回 1`，完全正常
- **index 一致性**：ifTable col 1/2/7/8/10 的 index 集合完全相同（`1..12, 130, 131`）
- **namelen 不變式**：開機 log 無違反
- **`agentx_owned.list`**：已從裝置移除
- **回歸**：`mainline − planE` = 13 筆，已定位並修正

那個 0.63 s 正是設計文件描述的慢路徑成本（fork+exec → ISS 寫檔 → snmpd 逐行解析）。Plan E 側 20 次全部 0.00，代表不再有 value-file 的快取命中/未命中之分。index 一致性那項特別重要，它證明 RW 欄位走轉發、RO 欄位走 subagent，兩者的 index 空間沒有分歧。

---

## 後續待辦 / 開放問題

- **燒錄含回歸修正的 image 並重驗** — 確認 13 筆回來、延遲未變差、`mainline − planE = ∅`
- **回歸修正尚未 commit** — 等 DUT 驗證通過再提交，避免未驗證的修正留在歷史
- **`.swu` 需要簽章步驟** — `sign_fwr.sh` 涉及外部簽章伺服器，由使用者執行
- **設計文件 §2.5 需重寫** — 「Plan C 補回 26 筆必要物件」的依據（七月基線）已證實不可靠
- **`8691.603` walk 在 `603.3.2.2.1.1.16.12` 中止** — 兩個分割區皆然，屬既有缺陷，本專案不修（D16）
- **101 筆「已實作但未宣告」+ `mxAcl`/`mxVa`** — 關閉或補 `.mib`，屬 RD/PM 決策，延至收尾階段
- **Task 6（整段註冊）條件性** — Phase 1 實測未觸發其必要條件，建議延至 Phase 3 與第二個 subagent 一併評估

最後一項值得說明：移除 `agentx_owned.list` 後不再有欄位被跳過，設計文件列的兩個風險（合成 index、GETNEXT 邊界）都失去觸發條件，整段註冊因此從「修正」變成「強化」。把它排到 Phase 3 才做，屆時第二個 subagent 存在，不變式才真正被考驗。

---

## 附錄 A：關鍵程式碼

### `->children` 鏈轉發（機制二）

```c
sub = netsnmp_subtree_find(reginfo->rootoid, reginfo->rootoid_len, NULL, ctx);
for ( c = sub->children; NULL != c; c = c->children )
{
    if ( c->reginfo && c->reginfo->handler &&
         c->reginfo->handler->access_method == agentx_master_handler )
        break;
}
if ( NULL == c ) return -1;          /* no subagent covers this OID */

saved_next    = request->next;
request->next = NULL;
(void) netsnmp_call_handlers(c->reginfo, reqinfo, request);
request->next = saved_next;
```

用 `reginfo->rootoid` 而非請求的 OID 定位 subtree：GETNEXT 時 `requestvb->name` 是前一個 OID，可能落在別的區段。

### 回歸修正：合成 index 不轉發

```c
/* ies_auto_mibs.c */
if ( !IES_FLAG_BITS_CHK(IES_FLAG_SNMP_SYNTHETIC_INDEX, entry->flags) )
{
    if ( mox_snmp_forward_get_to_subagent(reginfo, reqinfo, request) == 0 )
        return SNMP_ERR_NOERROR;
}

/* moxa_snmp_handle_util.c，CreateIndexEntry() 內，memcpy 之後 */
IES_FLAG_BITS_SET(IES_FLAG_SNMP_SYNTHETIC_INDEX, entryIndex->flags);
```

新旗標讓 `IES_FLAG_SNMP_MAX` 從 37 變 38，`IES_SNMP_FLAG_NUM` 仍是 5 bytes，packed 結構大小不變。

---

## 附錄 B：常用指令

```bash
# 建置（務必用 wrapper，直接 docker exec 會缺環境）
/usr/local/bin/run_in_docker.sh "make 3rdparty_net_snmp-rebuild ; make"

# 基線擷取（分段，因 602→603 交界會回 endOfMibView）
cd ~/WORK/SNMP_50ms && tools/capture_baseline.sh <label> 192.168.127.253

# 基線比對
python3 tools/walkdiff.py <mainline.txt> <planE.txt>

# 韌體分割區切換（partition 1 = Plan E，2 = NOS 7.0）
fw_setenv fwrbootpart 1 && reboot        # 或 /moxa/fwr_change.sh 然後 Y Y

# DUT console（partition 2 登入是 moxash，需 ~/mmtech_o 再 su）
bash ~/.claude/skills/dut-console/run.sh "<cmd>"
```

**注意**：`snmpwalk` / `snmpget` 在 PATH 上會被 `~/.local/bin` 的 pysnmp 版遮蔽，該版有 bug 會崩潰。一律使用 `/usr/bin/snmpwalk`。

---

## 附錄 C：產出檔案

| 檔案 | 內容 |
|---|---|
| `plan-e-agentx-get-offload-design-2026-08-03.md` | 設計文件，12 節，含 D1–D16 決策日誌 |
| `plan-e-phase0-1-PLAN-2026-08-03.md` | Phase 0/1 實作計畫，6 個 task / 38 步驟 |
| `tools/walkdiff.py` + `test_walkdiff.py` | 基線比對工具，6 個單元測試 |
| `tools/capture_baseline.sh` | 分段基線擷取，含 net-snmp 版本 guard |
| `snmpwalk/mainline-20260804_0318/` | 同日 mainline 基線（可重複使用） |
| `.superpowers/sdd/.../progress.md` | SDD ledger，含所有裁決與其理由 |
