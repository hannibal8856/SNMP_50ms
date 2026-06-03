# This is a test script for SNMP GET using pysnmp library.
# It retrieves multiple OIDs in one request and can handle both per-port and non-per-port OIDs.
# Modify the SNMP parameters and OID lists as needed for your testing.

import asyncio
import time
from time import sleep
from pysnmp.hlapi.v1arch.asyncio import *
#from pysnmp import debug

# Uncomment logger line to enable pysnmp debugging
#debug.set_logger(debug.Debug('all'))

# SNMP parameters
target = '192.168.127.253'  # SNMP Agent IP
snmp_port = 161             # SNMP Port
community = 'public'        # community string

# multiple OID
test_oids = [
    #('1.3.6.1.2.1.1.5.0', None),      # sysName.0
    #('1.3.6.1.2.1.2.2.1.8.1', None),    # ifOperStatus.1
    #('1.3.6.1.2.1.2.2.1.10.1', None),   # ifInOctets.1
    #('1.3.6.1.2.1.2.2.1.14.1', None),   # ifInErrors.1
    #('1.3.6.1.2.1.31.1.1.1.15.1', None), # ifHighSpeed.1
    ('1.3.6.1.4.1.8691.603.5.3.2.1.1.1.8.2', None), # fiberCheckStatTxPower.2
    ('1.3.6.1.4.1.8691.603.5.3.2.1.1.1.9.2', None), # fiberCheckStatRxPower.2
]

# These OIDs is_per_port=False
all_oids_column = [
    '1.3.6.1.2.1.2.2.1.8.8',    # ifOperStatus
    '1.3.6.1.2.1.2.2.1.10.8',   # ifInOctets
    '1.3.6.1.2.1.2.2.1.14.8',   # ifInErrors
    '1.3.6.1.2.1.31.1.1.1.15.8', # ifHighSpeed
    '1.3.6.1.4.1.8691.7.999.1.1.1.0', # cSysDateTime
    '1.3.6.1.4.1.8691.7.999.1.1.2.0', # cCPULoading
]

# These OIDs is_per_port=False
fix_port_oids_column = [
    '1.3.6.1.2.1.2.2.1.10.4',   # ifInOctets
    '1.3.6.1.2.1.2.2.1.14.4',   # ifInErrors
    '1.3.6.1.2.1.31.1.1.1.15.4', # ifHighSpeed
    '1.3.6.1.2.1.2.2.1.10.7',   # ifInOctets
    '1.3.6.1.2.1.2.2.1.14.7',   # ifInErrors
    '1.3.6.1.2.1.31.1.1.1.15.7', # ifHighSpeed
    '1.3.6.1.2.1.2.2.1.10.8',   # ifInOctets
    '1.3.6.1.2.1.2.2.1.14.8',   # ifInErrors
    '1.3.6.1.2.1.31.1.1.1.15.8', # ifHighSpeed
]


# These OIDs is_per_port=True
all_port_oids_column = [
    '1.3.6.1.2.1.2.2.1.8',    # ifOperStatus
    '1.3.6.1.2.1.2.2.1.10',   # ifInOctets
    '1.3.6.1.2.1.2.2.1.14',   # ifInErrors
    '1.3.6.1.2.1.31.1.1.1.15', # ifHighSpeed
]

# These OIDs is_per_port=True
link_status_oids_column = [
    '1.3.6.1.2.1.2.2.1.8',    # ifOperStatus
]

# These OIDs is_per_port=True
monitor_oids_column = [
    '1.3.6.1.2.1.2.2.1.10',   # ifInOctets
    '1.3.6.1.2.1.2.2.1.14',   # ifInErrors
    '1.3.6.1.2.1.31.1.1.1.15', # ifHighSpeed
]

# These OIDs is_per_port=True
# Note: first GET response time over 100ms
fiber_status_oids_column = [
    '1.3.6.1.4.1.8691.603.5.3.2.1.1.1.8', # fiberCheckStatTxPower
    '1.3.6.1.4.1.8691.603.5.3.2.1.1.1.9', # fiberCheckStatRxPower
    #'1.3.6.1.4.1.8691.603.5.3.2.1.1.1.12', # txPowerLimit
    #'1.3.6.1.4.1.8691.603.5.3.2.1.1.1.13', # rxPowerLimit
]

# These OIDs is_per_port=False
cv_oids_column = [
    #'1.3.6.1.4.1.8691.7.999.1.1.1.0', # cSysDateTime
    '1.3.6.1.4.1.8691.7.999.1.1.2.0', # cCPULoading
]

# Test parameters, modify them to change test settings
max_port = 4
test_times = 1
input_oids = fiber_status_oids_column
input_oids_is_per_port = True
test_snmp_version = 1  # 0 for v1, 1 for v2c

async def run(test_round, oids_column=monitor_oids_column, is_per_port=True):


    def combine_oid_tuple(oid_list, is_append_port, port_idx):
        # This function combine OIDs and method as tuple list for get_cmd() function.
        # If is_append_port is True, append target port_idx to the end of each target OID.
        # For example:
        #    input: ['1.3.6.1.4.1.8691.603.5.3.2.1.1.1.8', '1.3.6.1.4.1.8691.603.5.3.2.1.1.1.9']
        #   output: [('1.3.6.1.4.1.8691.603.5.3.2.1.1.1.8.5', None), ('1.3.6.1.4.1.8691.603.5.3.2.1.1.1.9.5', None)]
        full_oid_list = []
        for base_oid in oid_list:
            if is_append_port:
                full_oid_list.append((f"{base_oid}.{port_idx}", None))
            else:
                full_oid_list.append((base_oid, None))

        return full_oid_list

    snmpDispatcher = SnmpDispatcher()

    for port_index in range(max_port):

        all_oids_for_one_port_to_get = combine_oid_tuple(oids_column, is_per_port, port_index + 1)

        iterator = await getCmd(
            snmpDispatcher,
            CommunityData(community, mpModel=test_snmp_version),
            await UdpTransportTarget.create((target, snmp_port), timeout=2, retries=0),
            *all_oids_for_one_port_to_get,
        )

        errorIndication, errorStatus, errorIndex, varBinds = iterator

        if errorIndication:
            print(errorIndication)

        elif errorStatus:
            print(
                "{} at {}".format(
                    errorStatus.prettyPrint(),
                    errorIndex and varBinds[int(errorIndex) - 1][0] or "?",
                )
            )
        else:
            for varBind in varBinds:
                print(" = ".join([x.prettyPrint() for x in varBind]))

        # Only execute once if not per-port OID
        if is_per_port == False:
            break

    snmpDispatcher.transportDispatcher.closeDispatcher()

for round in range(test_times):
    asyncio.run(run(round, input_oids, input_oids_is_per_port))
    #time.sleep(1)

if input_oids_is_per_port:
    print(f"Test {test_times} times for {max_port} ports done.")
else:
    print(f"Test {test_times} times done.")
