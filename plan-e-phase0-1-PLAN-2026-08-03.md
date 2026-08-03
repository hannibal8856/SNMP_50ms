# Plan E — Phase 0 + Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用「整段註冊 + `->children` 鏈轉發」取代 Plan C 的 `agentx_owned.list` 與 probe-OID 機制，並以 ifTable / ifXTable 驗證行為不變。

**Architecture:** in-master（`ies-auto-mibs`）保留所有 OID 的註冊以承接 SET；`MODE_IS_GET` 時沿 `netsnmp_subtree` 的 `->children` 鏈找到 `agentx_master_handler` 的 reginfo 並轉發給 ISS AgentX subagent。路由變成結構性的，不再需要任何清單。

**Tech Stack:** C（net-snmp 5.9.3 mibgroup）、Buildroot 2023.02.11、Python 3（基線比對工具）、snmpwalk

## Global Constraints

- 平台只驗證 **Cortex-A9**；Marvell CN9130 不納入（設計文件 D13）
- 目標機型 **MDS-G4000-L3-4XGS**，DUT 位址 `192.168.127.253`，community `public`
- 對照組為 NOS mainline（`NOS_v7.0_develop`），實驗組為本分支 `snmp-plan-E2`
- **SET 路徑一行不得更動**：`MODE_IS_SET` 分支、`READ_CREATE_*`、`security_token`、rowStatus 全部維持原狀
- **不得引入 `ies_uri_handle_client` 或任何檔案式 IPC 到新程式碼**（D15）
- **不修 `8691.602`→`8691.603` 的 endOfMibView 缺陷**（D16）；基線以各子樹 root 分段擷取
- 設計文件：`~/WORK/SNMP_50ms/plan-e-agentx-get-offload-design-2026-08-03.md`。
  程式碼註解引用決策時使用 `per D<n>` 格式
- 每個 task 結束時 commit；C 端的變更 commit 在 `dl/3rdparty_net_snmp`（分支 `snmp-plan-E2`），
  工具與基線 commit 在 `~/WORK/SNMP_50ms`（分支 `develop`）

## 明確排除於本計畫之外

設計文件 §10.2 的 Phase 0 還包含下列項目，本計畫**不做**，理由是它們只有
framework subagent 用得到，與 Phase 1 的 ISS 側工作沒有相依：

| 項目 | 移往 |
|---|---|
| `.mk` 的 `--with-mib-modules` 加 `agentx/subagent` | Phase 3 計畫 |
| 建 `lib_moxa_ies_auto_mibs` + Rust FFI | Phase 3 計畫 |
| 建 `lib_moxa_snmp_agentx` + Rust FFI | Phase 3 計畫 |

本計畫只動 `dl/3rdparty_net_snmp`、`dl/plugin_moxa_snmp` 兩個 repo，
以及 `~/WORK/SNMP_50ms` 的工具與基線。

---

## File Structure

| 檔案 | 動作 | 職責 |
|---|---|---|
| `~/WORK/SNMP_50ms/tools/capture_baseline.sh` | 建立 | 分段擷取各子樹 walk，含完整性檢查 |
| `~/WORK/SNMP_50ms/tools/walkdiff.py` | 建立 | 解析 walk 檔、比對兩組基線、依 profile 分類差異 |
| `~/WORK/SNMP_50ms/tools/test_walkdiff.py` | 建立 | `walkdiff.py` 的 pytest 單元測試 |
| `dl/3rdparty_net_snmp/ies-auto-mibs/ies_auto_mibs.c` | 修改 | 移除 probe-OID 規則表與 `agentx_owned.list`，改用 children 轉發 |
| `dl/3rdparty_net_snmp/ies-auto-mibs/ies_auto_mibs_setup.c` | 修改 | 移除 Plan C 註解 |
| `dl/3rdparty_net_snmp/ies-auto-mibs/moxa_snmp_handle_util.c` | 修改 | 註冊時帶 priority；namelen 不變式監聽器 |
| `dl/plugin_moxa_snmp/Makefile` | 修改 | 移除 `agentx_owned.list` 安裝 |
| `dl/plugin_moxa_snmp/app/script/agentx_owned.list` | 刪除 | 機制廢除 |

**為什麼工具用 Python 而 C 端沒有單元測試**：`3rdparty_net_snmp` 沒有任何單元測試基礎設施
（`snmp_script/test/` 只有 ad-hoc 部署腳本），而 net-snmp mibgroup 的程式碼需要完整 agent
執行環境才能跑。C 端的驗證循環是「交叉編譯 → 部署 DUT → snmpwalk 比對基線」。
比對工具是這個循環的核心，因此它本身必須有單元測試——這是本計畫唯一能做真 TDD 的部分。

---

### Task 1: 基線比對工具

**Files:**
- Create: `~/WORK/SNMP_50ms/tools/walkdiff.py`
- Test: `~/WORK/SNMP_50ms/tools/test_walkdiff.py`

**Interfaces:**
- Produces:
  - `parse_walk(path: str) -> tuple[dict[str, str], bool]` — 回傳 `{oid: value_text}` 與
    「是否正常結束」（最後一行為 `End of MIB` 或 `No more variables` 則為 True）
  - `diff(a: dict, b: dict) -> tuple[set[str], set[str], list[tuple[str, str, str]]]` —
    回傳 `(only_in_a, only_in_b, value_differs)`，`value_differs` 為 `(oid, a_val, b_val)`
  - `load_profile(path: str) -> tuple[set[str], set[str]]` — 解析 `.profile`，
    回傳 `(private_mibs, standard_mibs)`

- [ ] **Step 1: 寫失敗的測試**

建立 `~/WORK/SNMP_50ms/tools/test_walkdiff.py`：

```python
import textwrap
import pytest
from walkdiff import parse_walk, diff, load_profile


def write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(textwrap.dedent(content).lstrip("\n"))
    return str(p)


def test_parse_walk_extracts_oid_and_value(tmp_path):
    f = write(tmp_path, "w.txt", """
        iso.3.6.1.2.1.1.1.0 = ""
        iso.3.6.1.2.1.2.2.1.1.1 = INTEGER: 1
        End of MIB
    """)
    oids, complete = parse_walk(f)
    assert oids == {
        "1.3.6.1.2.1.1.1.0": '""',
        "1.3.6.1.2.1.2.2.1.1.1": "INTEGER: 1",
    }
    assert complete is True


def test_parse_walk_flags_truncated_capture(tmp_path):
    f = write(tmp_path, "w.txt", """
        iso.3.6.1.2.1.1.1.0 = ""
        iso.3.6.1.2.1.2.2.1.1.1 = INTEGER: 1
    """)
    _, complete = parse_walk(f)
    assert complete is False


def test_diff_reports_all_three_categories():
    a = {"1.1": "INTEGER: 1", "1.2": "INTEGER: 2", "1.3": "INTEGER: 3"}
    b = {"1.2": "INTEGER: 99", "1.3": "INTEGER: 3", "1.4": "INTEGER: 4"}
    only_a, only_b, differs = diff(a, b)
    assert only_a == {"1.1"}
    assert only_b == {"1.4"}
    assert differs == [("1.2", "INTEGER: 2", "INTEGER: 99")]


def test_load_profile_splits_private_and_standard(tmp_path):
    f = write(tmp_path, "m.profile", """
        ### Private MIBs
        mxPort=YES
        mxLa=YES
        ### Standard MIBs
        IF-MIB=YES
        BRIDGE-MIB=YES
    """)
    private, standard = load_profile(f)
    assert private == {"mxPort", "mxLa"}
    assert standard == {"IF-MIB", "BRIDGE-MIB"}
```

- [ ] **Step 2: 執行測試確認失敗**

```bash
cd ~/WORK/SNMP_50ms/tools && python3 -m pytest test_walkdiff.py -v
```

預期：`ModuleNotFoundError: No module named 'walkdiff'`

- [ ] **Step 3: 寫最小實作**

建立 `~/WORK/SNMP_50ms/tools/walkdiff.py`：

```python
#!/usr/bin/env python3
"""Compare two snmpwalk captures and classify the differences.

Used as the verification loop for Plan E: every phase must show
`mainline - planE == empty` and no value drift on shared OIDs.
"""
import re
import sys

_LINE = re.compile(r"^\s*iso([0-9.]*)\s*=\s*(.*?)\s*$")
_TERMINATORS = ("End of MIB", "No more variables left in this MIB View")


def parse_walk(path):
    """Return ({oid: value_text}, completed_normally)."""
    oids = {}
    last = ""
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.strip():
                last = line.strip()
            m = _LINE.match(line)
            if m:
                oids["1" + m.group(1)] = m.group(2)
    return oids, any(last.startswith(t) for t in _TERMINATORS)


def diff(a, b):
    """Return (only_in_a, only_in_b, [(oid, a_value, b_value), ...])."""
    ka, kb = set(a), set(b)
    differs = [(o, a[o], b[o]) for o in sorted(ka & kb) if a[o] != b[o]]
    return ka - kb, kb - ka, differs


def load_profile(path):
    """Return (private_mib_names, standard_mib_names) from a product .profile."""
    private, standard, bucket = set(), set(), None
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("###"):
                bucket = private if "Private" in line else standard
                continue
            if "=" in line and bucket is not None:
                name, _, value = line.partition("=")
                if value.strip().upper() == "YES":
                    bucket.add(name.strip())
    return private, standard


def _sort_key(oid):
    return [int(x) for x in oid.split(".")]


def main(argv):
    if len(argv) != 3:
        print("usage: walkdiff.py <mainline.txt> <plane.txt>", file=sys.stderr)
        return 2
    a, a_ok = parse_walk(argv[1])
    b, b_ok = parse_walk(argv[2])
    for path, ok in ((argv[1], a_ok), (argv[2], b_ok)):
        if not ok:
            print(f"WARNING: {path} did not terminate normally "
                  f"(no 'End of MIB') - capture may be truncated")
    only_a, only_b, differs = diff(a, b)
    print(f"mainline={len(a)} planE={len(b)} "
          f"missing={len(only_a)} surplus={len(only_b)} value_differs={len(differs)}")
    for o in sorted(only_a, key=_sort_key):
        print(f"  MISSING  {o} = {a[o]}")
    for o in sorted(only_b, key=_sort_key):
        print(f"  SURPLUS  {o} = {b[o]}")
    for o, va, vb in differs:
        print(f"  DIFFERS  {o}: {va!r} -> {vb!r}")
    return 1 if (only_a or differs) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 4: 執行測試確認通過**

```bash
cd ~/WORK/SNMP_50ms/tools && python3 -m pytest test_walkdiff.py -v
```

預期：4 passed

- [ ] **Step 5: Commit**

```bash
cd ~/WORK/SNMP_50ms
git add tools/walkdiff.py tools/test_walkdiff.py
git commit -m "tools: add snmpwalk baseline comparison with unit tests"
```

---

### Task 2: 分段基線擷取腳本與取得基線

**Files:**
- Create: `~/WORK/SNMP_50ms/tools/capture_baseline.sh`
- Create: `~/WORK/SNMP_50ms/snmpwalk/<label>-<date>/` （擷取產物）

**Interfaces:**
- Consumes: Task 1 的 `walkdiff.py`
- Produces: 每個 label 一個目錄，內含各子樹 root 的 `.txt`，檔名為 root OID

**為什麼分段**：從 `1.3.6.1.4` 起跑的 walk 在 `8691.602.5.1.1.8.0` 之後即回 endOfMibView，
`8691.603.*` 拿不到（設計文件 §2.5(a)）。該缺陷屬 mainline，本專案不修（D16），
改以各 root 分別 walk。

- [ ] **Step 1: 建立擷取腳本**

建立 `~/WORK/SNMP_50ms/tools/capture_baseline.sh`：

```bash
#!/bin/bash
# Capture an SNMP baseline as separate per-subtree walks.
#
# Whole-tree walks stop at the 8691.602 -> 8691.603 boundary (mainline
# defect, not fixed by Plan E - see design doc D16), so each root is
# walked on its own.
#
# usage: capture_baseline.sh <label> <host> [community]
set -u

LABEL="${1:?usage: capture_baseline.sh <label> <host> [community]}"
HOST="${2:?usage: capture_baseline.sh <label> <host> [community]}"
COMM="${3:-public}"
OUT="$(dirname "$0")/../snmpwalk/${LABEL}-$(date +%Y%m%d_%H%M)"

ROOTS=(
    1.3.6.1.2                      # 標準 MIB（mib-2）
    1.3.6.1.4.1.8691.600           # Moxa 600
    1.3.6.1.4.1.8691.602           # Moxa 602（dlmod 地盤）
    1.3.6.1.4.1.8691.603           # Moxa 603（ISS / ies-auto-mibs 地盤）
    1.3.6.1.4.1.8691.605           # Moxa 605（L3）
    1.3.6.1.4.1.2021               # UCD-SNMP
    1.0.8802                       # LLDP
    1.3.111                        # IEEE 802.1
    1.2.840                        # PROFINET
)

mkdir -p "$OUT"
rc=0
for root in "${ROOTS[@]}"; do
    f="${OUT}/${root}.txt"
    echo "walking ${root} ..."
    snmpwalk -v2c -c "$COMM" -On -Cc "$HOST" "$root" > "$f" 2>&1
    n=$(grep -c "^\." "$f" 2>/dev/null || echo 0)
    if ! tail -1 "$f" | grep -qE "End of MIB|No more variables"; then
        echo "  !! ${root}: ${n} OIDs, 未正常結束 —— 此次擷取無效" >&2
        rc=1
    else
        echo "  ok ${root}: ${n} OIDs"
    fi
done
echo "輸出：${OUT}"
exit $rc
```

> `-On` 讓輸出為純數字 OID，`-Cc` 不因 OID 非遞增而中止（保留原始行為供檢查）。
> `walkdiff.py` 的 `parse_walk` 同時接受 `iso.` 與 `.1.3.` 兩種前綴——見 Step 2 的修正。

- [ ] **Step 2: 讓 `parse_walk` 同時支援數字 OID 格式**

`capture_baseline.sh` 用 `-On`，輸出前綴是 `.1.3.6...` 而非 `iso.3.6...`。
既有基線是 `iso.` 格式，兩者都要能解析。

先加測試到 `test_walkdiff.py`：

```python
def test_parse_walk_accepts_numeric_oid_format(tmp_path):
    f = write(tmp_path, "w.txt", """
        .1.3.6.1.2.1.1.1.0 = STRING: "sw"
        .1.3.6.1.2.1.2.2.1.1.1 = INTEGER: 1
        End of MIB
    """)
    oids, complete = parse_walk(f)
    assert oids == {
        "1.3.6.1.2.1.1.1.0": 'STRING: "sw"',
        "1.3.6.1.2.1.2.2.1.1.1": "INTEGER: 1",
    }
    assert complete is True
```

- [ ] **Step 3: 執行測試確認失敗**

```bash
cd ~/WORK/SNMP_50ms/tools && python3 -m pytest test_walkdiff.py -v -k numeric
```

預期：FAIL —— `oids` 為空 dict（`_LINE` 只匹配 `iso` 開頭）

- [ ] **Step 4: 修改 `walkdiff.py` 的解析規則**

把 `walkdiff.py` 的 `_LINE` 與 `parse_walk` 內的取值改為：

```python
_LINE = re.compile(r"^\s*(?:iso([0-9.]+)|\.?(1(?:\.[0-9]+)+))\s*=\s*(.*?)\s*$")
```

`parse_walk` 迴圈中的對應段落改為：

```python
            m = _LINE.match(line)
            if m:
                oid = ("1" + m.group(1)) if m.group(1) else m.group(2)
                oids[oid] = m.group(3)
```

- [ ] **Step 5: 執行全部測試確認通過**

```bash
cd ~/WORK/SNMP_50ms/tools && python3 -m pytest test_walkdiff.py -v
```

預期：5 passed（原 4 個仍須通過）

- [ ] **Step 6: 取得 mainline 基線**

在跑著 `NOS_v7.0_develop` image 的 DUT 上執行：

```bash
chmod +x ~/WORK/SNMP_50ms/tools/capture_baseline.sh
~/WORK/SNMP_50ms/tools/capture_baseline.sh mainline 192.168.127.253
```

預期：所有 root 都印 `ok`，離開碼 0。**任何 root 印 `!!` 就重跑，不得帶著不完整的基線往下走。**

- [ ] **Step 7: 取得 Plan C 現況基線**

換上目前 `snmp-plan-E2` 分支建出的 image，同樣執行：

```bash
~/WORK/SNMP_50ms/tools/capture_baseline.sh planC-before 192.168.127.253
```

- [ ] **Step 8: 記錄起點差異**

```bash
cd ~/WORK/SNMP_50ms
for f in snmpwalk/mainline-*/*.txt; do
    root=$(basename "$f")
    b="$(ls -d snmpwalk/planC-before-*)/${root}"
    echo "=== ${root} ==="
    python3 tools/walkdiff.py "$f" "$b"
done | tee snmpwalk/BASELINE-planC-before.txt
```

這份輸出就是後續每個 task 的比較基準：**`MISSING` 必須永遠是 0**，
`SURPLUS` 允許存在但不得增加（分類見設計文件 §2.6）。

- [ ] **Step 9: Commit**

```bash
cd ~/WORK/SNMP_50ms
git add tools/capture_baseline.sh tools/walkdiff.py tools/test_walkdiff.py \
        snmpwalk/mainline-* snmpwalk/planC-before-* snmpwalk/BASELINE-planC-before.txt
git commit -m "tools: add per-subtree baseline capture; record mainline and planC baselines"
```

---

### Task 3: 機制二 —— 以 `->children` 鏈轉發取代 probe-OID

**Files:**
- Modify: `dl/3rdparty_net_snmp/ies-auto-mibs/ies_auto_mibs.c:3062-3168`（規則表與轉發函式）
- Modify: `dl/3rdparty_net_snmp/ies-auto-mibs/ies_auto_mibs.c:3238-3250`（GET 分支的呼叫點）

**Interfaces:**
- Produces: `static int mox_snmp_forward_get_to_subagent(netsnmp_handler_registration *reginfo, netsnmp_agent_request_info *reqinfo, netsnmp_request_info *request)`
  —— 成功轉發回 `0`，找不到 AgentX 註冊回 `-1`（呼叫端回落本地慢路徑）

**設計依據：** D4（走 children 鏈）、D4a（接受 net-snmp 內部結構耦合）

- [ ] **Step 1: 刪除 probe-OID 規則表與舊轉發函式**

刪除 `ies_auto_mibs.c` 中從註解 `/* Plan C (agentx_owned.list): generic GET-forward table.` 起
到 `mox_snmp_forward_get_to_iss()` 函式結尾（含）的整段，亦即：

- `struct mox_iss_get_forward_rule` 定義
- `mox_col_ifadminstatus` / `mox_probe_ifindex` / `mox_col_iflinkupdowntrapen` /
  `mox_col_ifpromiscuousmode` / `mox_col_ifalias` / `mox_probe_ifname` 六個 oid 陣列
- `MOX_ISS_FWD_OIDLEN` 巨集與 `mox_iss_get_forward_rules[]`
- `mox_snmp_entry_iss_get_forward_rule()`
- `mox_snmp_forward_get_to_iss()`

- [ ] **Step 2: 加入新的轉發函式**

在同一位置寫入：

```c
/* GET 轉發（per D4）：in-master 保留註冊以承接 SET，MODE_IS_GET 時把請求交給
 * 覆蓋同一段 OID 的 AgentX subagent。
 *
 * net-snmp 的註冊表是二維的：->next/->prev 依 OID 順序把空間切成互不重疊的區段，
 * ->children 則掛著同一區段上所有重疊的註冊，依 (namelen 由長到短, priority 由小到大)
 * 排序（agent_registry.c 的 netsnmp_subtree_load()）。落敗者不會被丟棄。
 * netsnmp_subtree_split() 以 deepcopy 切割，name_a / namelen / reginfo 原樣保留，
 * 所以 subagent 的廣註冊被本地註冊切開後，切片仍帶原本的 reginfo 掛在 children 上。
 *
 * 這與 netsnmp_unregister_mib_context()（agent_registry.c:1705）尋找特定註冊的
 * 走訪方式相同，非自創手法。
 *
 * 以 reginfo->rootoid 而非 request 的 OID 定位 subtree：GETNEXT 時 requestvb->name
 * 是「前一個」OID，可能落在別的區段；用自己的註冊根才對 GET 與 GETNEXT 都成立。
 *
 * 回傳 0 = 已交給 subagent；-1 = 無 subagent 覆蓋，呼叫端回落本地慢路徑。 */
static int
mox_snmp_forward_get_to_subagent
(
    netsnmp_handler_registration *reginfo,
    netsnmp_agent_request_info   *reqinfo,
    netsnmp_request_info         *request
)
{
    netsnmp_subtree      *sub  = NULL;
    netsnmp_subtree      *c    = NULL;
    netsnmp_request_info *saved_next;
    const char           *ctx  = NULL;

    if ( NULL == reginfo || NULL == reginfo->rootoid )
    {
        return -1;
    }

    if ( NULL != reqinfo && NULL != reqinfo->asp && NULL != reqinfo->asp->pdu )
    {
        ctx = reqinfo->asp->pdu->contextName;
    }

    sub = netsnmp_subtree_find(reginfo->rootoid, reginfo->rootoid_len, NULL, ctx);
    if ( NULL == sub )
    {
        return -1;
    }

    for ( c = sub->children; NULL != c; c = c->children )
    {
        if ( NULL != c->reginfo && NULL != c->reginfo->handler &&
             c->reginfo->handler->access_method == agentx_master_handler )
        {
            break;
        }
    }

    if ( NULL == c )
    {
        return -1;                  /* 無 subagent 覆蓋此 OID */
    }

    /* 只轉發這一筆；不解開 next 會讓 netsnmp_call_handlers 一併拉走整條 chain。 */
    saved_next    = request->next;
    request->next = NULL;
    (void) netsnmp_call_handlers(c->reginfo, reqinfo, request);
    request->next = saved_next;

    return 0;
}
```

- [ ] **Step 3: 加入必要的標頭**

`netsnmp_subtree` 與 `netsnmp_subtree_find()` 已經可用——被刪掉的 `mox_snmp_forward_get_to_iss()`
就在用它們，所以既有的 include 已足夠。**唯一要新增的是 `agentx_master_handler` 的宣告**，
它在 `agent/mibgroup/agentx/master.h`。`ies-auto-mibs` 在建置時被 symlink 進
`net-snmp-5.9.3/agent/mibgroup/`（見 `3rdparty_net_snmp.mk` 的
`ln -sf $(@D)/ies-auto-mibs $(@D)/$(NET_SNMP_DIR)/agent/mibgroup`），因此相對路徑成立。

在 `ies_auto_mibs.c` 既有的 `#include` 區塊末端加入：

```c
#include "agentx/master.h"
```

- [ ] **Step 4: 改寫 GET 分支的呼叫點**

把 `mox_snmp_handle_entry()` 中 `MODE_IS_GET(reqinfo->mode)` 區塊開頭的這一段：

```c
        /* Plan C (agentx_owned.list): RW column GET → ISS AgentX (fast); SET stays local. */
        {
            const struct mox_iss_get_forward_rule *fwd_rule =
                mox_snmp_entry_iss_get_forward_rule(entry);
            if ( fwd_rule != NULL )
            {
                if ( mox_snmp_forward_get_to_iss(fwd_rule, reqinfo, request) == 0 )
                {
                    return SNMP_ERR_NOERROR;
                }
                /* ISS unavailable → fall through to the local (slow) path */
            }
        }
```

換成：

```c
        /* per D2：GET 一律交給 subagent，不分 RO/RW、不看清單。
         * subagent 未連線或未覆蓋此 OID 時回落下方的本地慢路徑。 */
        if ( mox_snmp_forward_get_to_subagent(reginfo, reqinfo, request) == 0 )
        {
            return SNMP_ERR_NOERROR;
        }
```

- [ ] **Step 5: 交叉編譯**

```bash
cd ~/SNMP_PLAN_E/buildroot
make 3rdparty_net_snmp-rebuild 2>&1 | tee /tmp/build-task3.log
```

預期：離開碼 0。確認日誌中確實編譯的是 Cortex-A9 目標：

```bash
grep -E "arm-.*-gcc|Cortex|cortex" /tmp/build-task3.log | head -3
```

若出現 `agentx_master_handler` 未宣告，檢查 Step 3 的 include 是否加對位置
（必須在 net-snmp 的 agent 標頭之後）。

- [ ] **Step 6: 部署並驗證**

把新的 image 燒進 DUT，然後：

```bash
~/WORK/SNMP_50ms/tools/capture_baseline.sh task3 192.168.127.253
cd ~/WORK/SNMP_50ms
for f in snmpwalk/mainline-*/*.txt; do
    root=$(basename "$f")
    python3 tools/walkdiff.py "$f" "$(ls -d snmpwalk/task3-*)/${root}"
done | tee snmpwalk/RESULT-task3.txt
grep -c MISSING snmpwalk/RESULT-task3.txt
```

預期：`MISSING` 筆數為 **0**；`SURPLUS` 與 `DIFFERS` 不得多於 `BASELINE-planC-before.txt`。

- [ ] **Step 7: 確認轉發真的生效**

轉發若整段失效會回落慢路徑，walk 結果仍正確但延遲會回到 600ms 級。實測 ifTable：

```bash
for i in $(seq 1 20); do
  /usr/bin/time -f %e snmpget -v2c -c public 192.168.127.253 \
    .1.3.6.1.2.1.2.2.1.8.1 2>&1 >/dev/null
done | sort -n | tail -1
```

預期：p100 < 0.05 秒。若明顯大於此值，轉發沒有生效——檢查 `sub->children` 是否為 NULL
（表示 ISS 尚未完成 AgentX 註冊，或本地註冊的 namelen 使兩者未落在同一條 children 鏈）。

- [ ] **Step 8: Commit**

```bash
cd ~/SNMP_PLAN_E/buildroot/dl/3rdparty_net_snmp
git add ies-auto-mibs/ies_auto_mibs.c
git commit -m "ies-auto-mibs: forward GET via subtree children chain

Replaces the probe-OID table (per D4). The probe approach needed an
ISS-owned RO instance in the same registration region, which is an
accident of each table's shape and does not generalise past the four
hardcoded rules. Walking reginfo->rootoid's children chain finds the
AgentX registration directly, works for GET and GETNEXT alike, and
needs no per-table data."

cd ~/WORK/SNMP_50ms
git add snmpwalk/task3-* snmpwalk/RESULT-task3.txt
git commit -m "baseline: task3 result (children-chain forwarding)"
```

---

### Task 4: 廢除 `agentx_owned.list`

**Files:**
- Modify: `dl/3rdparty_net_snmp/ies-auto-mibs/ies_auto_mibs.c:3362-3413`（清單載入與判定）
- Modify: `dl/3rdparty_net_snmp/ies-auto-mibs/ies_auto_mibs.c`（`mox_snmp_init_entry` 內的 skip）
- Modify: `dl/3rdparty_net_snmp/ies-auto-mibs/ies_auto_mibs_setup.c:290-293`（Plan C 註解）
- Modify: `dl/plugin_moxa_snmp/Makefile:59-61`
- Delete: `dl/plugin_moxa_snmp/app/script/agentx_owned.list`

**Interfaces:**
- Consumes: Task 3 的 `mox_snmp_forward_get_to_subagent()`
- Produces: 無新介面。所有 entry 恢復本地註冊，路由完全由 §4.3 的機制決定

**設計依據：** D1

**為什麼此時才移除**：Task 3 先讓轉發對「本地有註冊」的 OID 生效；本 task 再讓所有 OID
都回到本地註冊。順序相反的話，中間狀態會有一批 RO OID 既不在本地也不轉發。

- [ ] **Step 1: 刪除清單機制**

刪除 `ies_auto_mibs.c` 中：

- `AGENTX_OWNED_LIST_FILE` / `AGENTX_OWNED_MAX` / `AGENTX_OWNED_PREFIX_LEN` 三個巨集
- `gAgentXOwnedPrefix` / `gAgentXOwnedCount` / `gAgentXOwnedLoaded` 三個全域
- `mox_snmp_load_agentx_owned_list()`
- `mox_snmp_entry_is_agentx_owned()`
- 上述整段前的 `/* Plan C: agentx_owned.list — RO entries whose URI prefix ... */` 註解

- [ ] **Step 2: 移除 `mox_snmp_init_entry()` 內的 skip**

把這一段整個刪除：

```c
        /* Plan C: RO + agentx_owned prefix → skip local register, let snmpd
         * master route this OID to the ISS AgentX subagent. */
        if ( !gAgentXOwnedLoaded )
        {
            mox_snmp_load_agentx_owned_list();
            gAgentXOwnedLoaded = 1;
        }
        if ( mox_snmp_entry_is_agentx_owned(entry) )
        {
            free(entry);
            continue;
        }
```

刪除後 `ies_auto_mibs_setup_entry_flags()` 之後就直接接
`moxaSnmpHandle_UtilMibEntryGenerateTableIndexEntry(entry)`。

- [ ] **Step 3: 更新 `ies_auto_mibs_setup.c` 的註解**

把 `ifmibdb_MibEntry` 前的三行 Plan C 註解：

```c
    /* Plan C: register ifmibdb normally; agentx_owned.list (read in
       mox_snmp_init_entry) decides per-column — RO ifTable/ifXTable columns are
       skipped (→ ISS subagent), RW (ifAdminStatus) stays local (→ framework SET). */
```

換成：

```c
    /* per D1/D2：全部欄位一律本地註冊以承接 SET；GET 由
       mox_snmp_forward_get_to_subagent() 統一轉給 AgentX subagent。 */
```

- [ ] **Step 4: 移除安裝步驟並刪檔**

`dl/plugin_moxa_snmp/Makefile` 刪除這三行：

```make
	# Plan C: agentx_owned.list (delegation whitelist read by ies-auto-mibs)
	${SILENCE}mkdir -p ${DESTDIR}/etc/moxa/netsnmp
	${SILENCE}install -m 644 ${APP_SCRIPT_PATH}/agentx_owned.list ${DESTDIR}/etc/moxa/netsnmp/agentx_owned.list
```

> `mkdir -p ${DESTDIR}/etc/moxa/netsnmp` 若被 Makefile 其他步驟依賴則保留該行，
> 只刪 `install` 與註解。刪除前先確認：
> `grep -n "etc/moxa/netsnmp" dl/plugin_moxa_snmp/Makefile`

然後：

```bash
cd ~/SNMP_PLAN_E/buildroot/dl/plugin_moxa_snmp
git rm app/script/agentx_owned.list
```

- [ ] **Step 5: 交叉編譯**

```bash
cd ~/SNMP_PLAN_E/buildroot
make 3rdparty_net_snmp-rebuild plugin_moxa_snmp-rebuild 2>&1 | tee /tmp/build-task4.log
```

預期：離開碼 0，且 `/tmp/build-task4.log` 無 `agentx_owned` 相關警告。

- [ ] **Step 6: 確認舊檔不再進 image**

```bash
find ~/SNMP_PLAN_E/buildroot/output/target -name "agentx_owned.list"
```

預期：無輸出。

- [ ] **Step 7: 部署並驗證**

```bash
~/WORK/SNMP_50ms/tools/capture_baseline.sh task4 192.168.127.253
cd ~/WORK/SNMP_50ms
for f in snmpwalk/mainline-*/*.txt; do
    root=$(basename "$f")
    python3 tools/walkdiff.py "$f" "$(ls -d snmpwalk/task4-*)/${root}"
done | tee snmpwalk/RESULT-task4.txt
```

預期：`MISSING` 為 0。**特別檢查 `ifIndex` 與 `ifName` 仍存在**——設計文件 §2.5 指出
mainline 缺這兩欄而 Plan C 補回，本 task 讓 col 1 恢復本地註冊，必須確認補回的效果沒有消失：

```bash
snmpwalk -v2c -c public -On 192.168.127.253 .1.3.6.1.2.1.2.2.1.1 | wc -l
snmpwalk -v2c -c public -On 192.168.127.253 .1.3.6.1.2.1.31.1.1.1.1 | wc -l
```

預期：各 13 筆。**若變成 0，代表本地 col-1 handler 又回到「取不到值」的舊行為
（`MAKEUP_INDEX_FOR_ISS` 對 ifTable 失效），此時必須停下來**——這會是 Task 6
（整段註冊）真正必要的證據，回報後再決定。

- [ ] **Step 8: 驗證 SET 未受影響**

```bash
snmpset -v2c -c private 192.168.127.253 .1.3.6.1.2.1.2.2.1.7.1 i 2
snmpget -v2c -c public  192.168.127.253 .1.3.6.1.2.1.2.2.1.7.1
snmpset -v2c -c private 192.168.127.253 .1.3.6.1.2.1.2.2.1.7.1 i 1
snmpget -v2c -c public  192.168.127.253 .1.3.6.1.2.1.2.2.1.7.1
```

預期：`ifAdminStatus.1` 隨 SET 在 2 與 1 之間變化，且 GET 讀得到剛寫入的值。

- [ ] **Step 9: Commit**

```bash
cd ~/SNMP_PLAN_E/buildroot/dl/3rdparty_net_snmp
git add ies-auto-mibs/ies_auto_mibs.c ies-auto-mibs/ies_auto_mibs_setup.c
git commit -m "ies-auto-mibs: drop agentx_owned.list, register every entry locally

Per D1. The list keyed on entry->uri while routing happens in OID space,
and the two do not correspond: mxLadb's OIDs live under 8691.603.1.2 but
its URIs are fscfadb/ and fsladb/, which ISS keeps under the
AgentX-excluded 2076 tree, so listing them would have produced
noSuchObject rather than speed. Routing is now structural - every entry
registers locally to own SET, and GET forwards through the children
chain - so there is no list to keep in sync."

cd ~/SNMP_PLAN_E/buildroot/dl/plugin_moxa_snmp
git add Makefile
git commit -m "plugin_moxa_snmp: stop installing agentx_owned.list (per D1)"

cd ~/WORK/SNMP_50ms
git add snmpwalk/task4-* snmpwalk/RESULT-task4.txt
git commit -m "baseline: task4 result (agentx_owned.list removed)"
```

---

### Task 5: namelen 不變式監聽器

**Files:**
- Modify: `dl/3rdparty_net_snmp/ies-auto-mibs/moxa_snmp_handle_util.c`（新增監聽器與註冊）
- Modify: `dl/3rdparty_net_snmp/ies-auto-mibs/ies_auto_mibs.c`（init 時掛上監聽器）

**Interfaces:**
- Produces:
  - `int moxaSnmpHandle_UtilAgentxRegisterWatch(int majorID, int minorID, void *serverarg, void *clientarg)`
    —— `SNMPD_CALLBACK_REGISTER_OID` 的回呼，永遠回 `0`
  - `void moxaSnmpHandle_UtilInstallAgentxRegisterWatch(void)` —— 掛上回呼

**設計依據：** D4b

**要防的是什麼**：subtree 排序鍵是 (namelen 由長到短, priority 由小到大)，**namelen 優先於
priority**。任何 subagent 只要註冊得比 in-master 細（OID 更長），就會直接搶下 children 鏈頭，
使 in-master 的 handler 完全不被呼叫——SET 會被送到 subagent、GET 轉發也不會執行。
症狀是「某些 OID 的 SET 突然不生效且無錯誤訊息」，極難查。

- [ ] **Step 1: 加入監聽器**

在 `moxa_snmp_handle_util.c` 檔案末端（`#endif` 之前）加入：

```c
/* per D4b：subtree 排序鍵是 (namelen 由長到短, priority 由小到大)，namelen 先比。
 * 任何 subagent 若註冊得比 in-master 細，就會搶下 children 鏈頭，使 in-master 的
 * handler 不被呼叫（SET 落到 subagent、GET 轉發不執行）。priority 救不了這種情況。
 *
 * 不變式：in-master 在每一個 subagent 覆蓋的 OID 範圍上，namelen 必須 >= 該 subagent。
 * 這裡在 subagent 註冊時檢查並記 log，違反時給出可查的線索而非靜默失敗。 */
int
moxaSnmpHandle_UtilAgentxRegisterWatch
(
    int majorID, int minorID, void *serverarg, void *clientarg
)
{
    struct register_parameters *rp  = (struct register_parameters *) serverarg;
    netsnmp_subtree            *sub = NULL;

    (void) majorID; (void) minorID; (void) clientarg;

    if ( NULL == rp || NULL == rp->name || NULL == rp->reginfo ||
         NULL == rp->reginfo->handler )
    {
        return 0;
    }

    /* 只看 AgentX subagent 的註冊 */
    if ( rp->reginfo->handler->access_method != agentx_master_handler )
    {
        return 0;
    }

    sub = netsnmp_subtree_find(rp->name, rp->namelen, NULL, rp->contextName);
    if ( NULL == sub )
    {
        snmp_log(LOG_WARNING,
                 "moxa: AgentX registered an OID range with no in-master "
                 "registration; SET on it will not reach ies-auto-mibs\n");
        return 0;
    }

    if ( sub->namelen < rp->namelen )
    {
        snmp_log(LOG_ERR,
                 "moxa: AgentX registration namelen=%u is finer than in-master "
                 "namelen=%u for the same range; in-master handler will be "
                 "bypassed (SET breaks silently). See design doc D4b.\n",
                 (unsigned) rp->namelen, (unsigned) sub->namelen);
    }

    return 0;
}

void
moxaSnmpHandle_UtilInstallAgentxRegisterWatch(void)
{
    snmp_register_callback(SNMP_CALLBACK_APPLICATION,
                           SNMPD_CALLBACK_REGISTER_OID,
                           moxaSnmpHandle_UtilAgentxRegisterWatch,
                           NULL);
}
```

- [ ] **Step 2: 宣告與掛載**

`moxa_snmp_handle_util.h` 加入宣告：

```c
int  moxaSnmpHandle_UtilAgentxRegisterWatch(int majorID, int minorID,
                                            void *serverarg, void *clientarg);
void moxaSnmpHandle_UtilInstallAgentxRegisterWatch(void);
```

在 `ies_auto_mibs.c` 的 `init_ies_auto_mibs()` 中，於 `ies_auto_mibs_load_mibs()`
呼叫**之前**加入：

```c
    moxaSnmpHandle_UtilInstallAgentxRegisterWatch();   /* per D4b */
```

`moxa_snmp_handle_util.c` 需要的標頭（若尚未包含）：

```c
#include <net-snmp/agent/agent_callbacks.h>   /* SNMPD_CALLBACK_REGISTER_OID */
#include <net-snmp/agent/agent_registry.h>    /* struct register_parameters, netsnmp_subtree_find */
#include "agentx/master.h"                    /* agentx_master_handler */
```

`agentx/master.h` 在此處同樣靠 symlink 進 `agent/mibgroup/` 才解析得到，
與 Task 3 Step 3 相同。

- [ ] **Step 3: 交叉編譯**

```bash
cd ~/SNMP_PLAN_E/buildroot
make 3rdparty_net_snmp-rebuild 2>&1 | tee /tmp/build-task5.log
```

預期：離開碼 0。

- [ ] **Step 4: 部署並確認開機 log 乾淨**

部署後在 DUT 上：

```bash
grep -i "moxa: AgentX" /var/log/messages /moxa/permanent/moxalog/* 2>/dev/null
```

預期：**無輸出**。ISS 註冊的是 MIB root（namelen 短），in-master 是逐欄位（namelen 長），
不變式成立。若出現 `namelen is finer` 訊息，代表有 subagent 註冊得比 in-master 細，
必須先釐清該段 OID 的歸屬再往下走。

- [ ] **Step 5: 回歸驗證**

```bash
~/WORK/SNMP_50ms/tools/capture_baseline.sh task5 192.168.127.253
cd ~/WORK/SNMP_50ms
for f in snmpwalk/mainline-*/*.txt; do
    root=$(basename "$f")
    python3 tools/walkdiff.py "$f" "$(ls -d snmpwalk/task5-*)/${root}"
done | tee snmpwalk/RESULT-task5.txt
```

預期：與 Task 4 結果相同（監聽器只記 log，不改行為）。

- [ ] **Step 6: Commit**

```bash
cd ~/SNMP_PLAN_E/buildroot/dl/3rdparty_net_snmp
git add ies-auto-mibs/moxa_snmp_handle_util.c ies-auto-mibs/moxa_snmp_handle_util.h \
        ies-auto-mibs/ies_auto_mibs.c
git commit -m "ies-auto-mibs: warn when a subagent registers finer than in-master

Per D4b. The subtree sort key is (namelen desc, priority asc), so namelen
wins outright: a subagent registering a longer OID than in-master takes
the chain head and the in-master handler stops being called, which breaks
SET with no error anywhere. Lowering in-master's priority does not help.
This hooks SNMPD_CALLBACK_REGISTER_OID so the violation shows up in the
boot log instead of as silently broken writes."

cd ~/WORK/SNMP_50ms
git add snmpwalk/task5-* snmpwalk/RESULT-task5.txt
git commit -m "baseline: task5 result (namelen invariant watchdog)"
```

---

### Task 6: 整段 OID 註冊（**條件性**）

**設計依據：** D3

**執行條件：** 只有在 Task 4 Step 7 出現下列任一情況時才執行本 task：

- `ifIndex`（`.1.3.6.1.2.1.2.2.1.1`）或 `ifName`（`.1.3.6.1.2.1.31.1.1.1.1`）的筆數掉回 0
- `RESULT-task4.txt` 的 `MISSING` 不為 0
- Task 5 的開機 log 出現 namelen 不變式違反

**若上述皆未發生**：`agentx_owned.list` 已廢除，不再有欄位被跳過，
設計文件 §3.3 的合成 index 風險與 §4.3 的 GETNEXT 邊界風險都**沒有觸發條件**。
此時整段註冊的價值是「面對未來更細粒度的 subagent 時更穩健」，屬強化而非修正，
**應延到 framework subagent（Phase 3）那份計畫一併評估**，理由是屆時才會出現
第二個 subagent、不變式才真正被考驗。

- [ ] **Step 1: 記錄判定結果**

無論走哪條路，都要把判定寫進設計文件的決策日誌，讓後續接手的人知道 D3 的狀態：

```bash
cd ~/WORK/SNMP_50ms
# 在 plan-e-agentx-get-offload-design-2026-08-03.md 的 §11 決策日誌新增一列：
#   | D17 | Phase 1 不實作整段註冊（D3 延後至 Phase 3） | Task 4 移除 agentx_owned.list
#     後不再有欄位被跳過，§3.3 合成 index 與 §4.3 GETNEXT 邊界兩個風險均無觸發條件；
#     實測 ifIndex/ifName 仍為 13 筆、MISSING=0、無不變式違反 | 立即實作：在風險未
#     出現前投入大型重構，且第二個 subagent 尚未存在，不變式無從驗證 |
git add plan-e-agentx-get-offload-design-2026-08-03.md
git commit -m "design: record D17 (whole-subtree registration deferred to Phase 3)"
```

若判定為「需要執行」，改為記錄 D17 的相反結論與觸發它的實測證據，
並在本計畫下方追加整段註冊的實作 task 後再繼續。

---

## 完成條件

Phase 0 + Phase 1 視為完成，當且僅當：

1. `~/WORK/SNMP_50ms/tools/` 下有可重跑的基線擷取與比對工具，且比對工具的單元測試全數通過
2. mainline 與 Plan E 的分段基線都已取得且**每個 root 都正常結束**
3. `RESULT-task5.txt` 的 `MISSING` 為 0，`SURPLUS` 與 `DIFFERS` 不多於 `BASELINE-planC-before.txt`
4. `ifAdminStatus` 的 GET p100 < 50 ms
5. `ifIndex` / `ifName` 各 13 筆仍在
6. `ifAdminStatus` 的 SET 行為與 mainline 一致
7. DUT 開機 log 無 namelen 不變式違反
8. `agentx_owned.list` 不在 image 中，程式碼中無 probe-OID 規則表
9. D17 已記入設計文件決策日誌
