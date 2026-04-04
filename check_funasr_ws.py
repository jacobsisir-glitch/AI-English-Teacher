import asyncio
import websockets

async def main():
    try:
        ws = await websockets.connect("ws://127.0.0.1:10095")
        print("CONNECTED")
        await ws.close()
    except Exception as e:
        print("FAILED:", type(e).__name__, str(e))

asyncio.run(main())
