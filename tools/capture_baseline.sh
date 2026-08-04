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

# PATH may resolve snmpwalk to the pysnmp/snmpclitools implementation, which
# crashes partway through a walk. Default to the net-snmp binary; override
# with SNMPWALK=... if it lives elsewhere.
SNMPWALK="${SNMPWALK:-/usr/bin/snmpwalk}"

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

if ! "$SNMPWALK" --version 2>&1 | grep -q "NET-SNMP"; then
    echo "!! $SNMPWALK is not the net-snmp binary; set SNMPWALK to the right path" >&2
    exit 1
fi

mkdir -p "$OUT"
rc=0
for root in "${ROOTS[@]}"; do
    f="${OUT}/${root}.txt"
    e="${OUT}/${root}.err"
    echo "walking ${root} ..."
    # stderr must NOT be merged into $f. snmpwalk writes its error text the
    # moment it happens, which lands mid-line in the OID stream and splits one
    # OID across two lines; neither half then matches walkdiff's OID pattern,
    # so a present OID is reported missing. Observed 2026-08-04 on 8691.603
    # column 13 port 9, which cost an investigation.
    "$SNMPWALK" -v2c -c "$COMM" -On -Cc "$HOST" "$root" > "$f" 2> "$e"
    walk_rc=$?
    n=$(grep -c "^\." "$f")
    err=""
    if [ -s "$e" ]; then
        err="  <- $(head -1 "$e")"
    fi

    # A scoped walk can exit non-zero after producing a complete, error-free
    # capture, so the exit status alone does not condemn it. Judge on content.
    if [ "$n" -eq 0 ]; then
        if [ "$walk_rc" -ne 0 ] || [ -s "$e" ]; then
            echo "  !! ${root}: 0 OIDs 且 snmpwalk 報錯（exit ${walk_rc}）—— 此次擷取無效${err}" >&2
            rc=1
        else
            echo "  -- ${root}: 0 OIDs（此裝置未實作此子樹）"
        fi
    elif [ -s "$e" ]; then
        # 8691.602 and 8691.603 abort mid-walk on mainline too - a known device
        # limitation, not a capture failure. The OIDs already collected are
        # usable; only the reach of the walk is in doubt.
        echo "  ~~ ${root}: ${n} OIDs，但走到一半中止 —— 內容可用，涵蓋範圍可能不完整${err}"
    else
        echo "  ok ${root}: ${n} OIDs"
    fi
done
echo "輸出：${OUT}"
exit $rc
