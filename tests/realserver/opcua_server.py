"""An OPC UA server of asyncua's (not ours), for the realserver tests: binds 127.0.0.1 only.

    python opcua_server.py PORT

Prints "READY <namespace index>" when it serves, then changes its variables every 0.2 s
until it is killed: Press.Speed (Double), Press.Count (Int32), Press.Running (Boolean),
Press.Recipe (String), and Press.Fault, whose status is Bad.
"""
import asyncio
import sys

from asyncua import Server, ua


async def main(port: int) -> None:
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}/plant")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    index = await server.register_namespace("urn:naga:realserver")
    objects = server.nodes.objects

    async def variable(name, value, kind):
        return await objects.add_variable(ua.NodeId(f"Press.{name}", index), name,
                                          ua.Variant(value, kind))
    speed = await variable("Speed", 10.0, ua.VariantType.Double)
    count = await variable("Count", 0, ua.VariantType.Int32)
    running = await variable("Running", True, ua.VariantType.Boolean)
    recipe = await variable("Recipe", "A-1", ua.VariantType.String)
    fault = await variable("Fault", 0.0, ua.VariantType.Double)
    async with server:
        await fault.write_value(ua.DataValue(ua.Variant(0.0, ua.VariantType.Double),
                                             StatusCode=ua.StatusCode(ua.StatusCodes.BadSensorFailure)))
        sys.stdout.write(f"READY {index}\n")
        sys.stdout.flush()
        n = 0
        while True:
            n += 1
            await speed.write_value(ua.Variant(10.0 + n * 0.5, ua.VariantType.Double))
            await count.write_value(ua.Variant(-n, ua.VariantType.Int32))
            await running.write_value(ua.Variant(n % 2 == 0, ua.VariantType.Boolean))
            await recipe.write_value(ua.Variant(f"R-{n}", ua.VariantType.String))
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
