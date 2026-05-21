# This is a test script for SNMP GET using pysnmp library.
# It retrieves multiple OIDs in one request and can handle both per-port and non-per-port OIDs.
# Modify the SNMP parameters and OID lists as needed for your testing.

import asyncio
import time
import sys
import signal
from time import sleep
from pysnmp.hlapi.v1arch.asyncio import *
#from pysnmp import debug

# Uncomment logger line to enable pysnmp debugging
#debug.set_logger(debug.Debug('all'))

# SNMP parameters
target = '192.168.127.253'  # SNMP Agent IP
snmp_port = 161             # SNMP Port
community = 'public'        # community string

IS_PER_PORT = True
NOT_PER_PORT = False

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

group1 = (cv_oids_column, NOT_PER_PORT)
group2 = (link_status_oids_column, IS_PER_PORT)
group3 = (monitor_oids_column, IS_PER_PORT)
group4 = (fiber_status_oids_column, IS_PER_PORT)

test_oids = (group1, group2, group3, group4)

# Test parameters, modify them to change test settings
# test_times = 1
# input_oids = fiber_status_oids_column
# input_oids_is_per_port = True
max_port = 28
SEVEN_DAYS_IN_MINUTES = 7 * 24 * 60
THREE_DAYS_IN_MINUTES = 3 * 24 * 60
ONE_DAY_IN_MINUTES = 1 * 24 * 60
test_duration_minutes = THREE_DAYS_IN_MINUTES
request_interval_seconds = 30
test_snmp_version = 1  # 0 for v1, 1 for v2c

def signal_handler(sig, frame):
    print('\nExit...')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

async def send_snmp_request(oids_column=monitor_oids_column, is_per_port=True):

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

    with SnmpDispatcher() as snmpDispatcher:
        
        for port_index in range(max_port):
        
            all_oids_for_one_port_to_get = combine_oid_tuple(oids_column, is_per_port, port_index + 1)

            iterator = await get_cmd(
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

def print_test_parameters():
    #print initial test parameters
    print(f"Test port: 1-{max_port}")
    if test_duration_minutes == SEVEN_DAYS_IN_MINUTES:
        print("Test duration: 7 days")
    elif test_duration_minutes == THREE_DAYS_IN_MINUTES:
        print("Test duration: 3 days")
    elif test_duration_minutes == ONE_DAY_IN_MINUTES:
        print("Test duration: 1 day")
    else:
        print(f"Test duration: {test_duration_minutes} mins")
    print(f"Request interval: {request_interval_seconds} secs")

print_test_parameters()
# delay seconds for users to check initial parameters
START_DELAY_SECONDS = 2
print(f"Test will start in {START_DELAY_SECONDS} seconds... Ctrl+C to abort.")
time.sleep(START_DELAY_SECONDS)

round = 1
start_time = time.time()
end_time = start_time + 60 * test_duration_minutes
while time.time() < end_time:

    for oids, is_per_port in test_oids:
        # print(f"Round {round}: Testing OIDs {oids} is_per_port={is_per_port}")
        asyncio.run(send_snmp_request(oids, is_per_port))
        print()

    print(f"Round {round} done")
    print()
    round = round + 1

    time.sleep(request_interval_seconds)
print("Test completed.")

