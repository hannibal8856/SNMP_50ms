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
