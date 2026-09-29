"""Print the GATT services of an excited iTAG X30, to find the service UUID for x30_provisioner.html."""
import asyncio
from bleak import BleakClient, BleakScanner

XA_CHAR = '51f1f198-4321-9abc-4321-0067eaa0da40'


async def main():
    print('Scanning 15 s for an excited X30 (hold the rear button until it flashes blue)...')
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or d.name or '').startswith('iTAG X30'), timeout=15)
    if dev is None:
        print('No X30 advertising.')
        return 1
    print('Found', dev.name, dev.address)
    async with BleakClient(dev.address, timeout=15) as client:
        for svc in client.services:
            marker = '  <-- holds the XA characteristic' if any(
                c.uuid.lower() == XA_CHAR for c in svc.characteristics) else ''
            print('service', svc.uuid, marker)
            for ch in svc.characteristics:
                print('    char', ch.uuid, ','.join(ch.properties))
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
