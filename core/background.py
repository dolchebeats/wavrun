"""Read-only network work that must not hold Python open on shutdown."""
import asyncio
import threading


async def background_read(function, cancel_event=None):
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def deliver(value, error):
        if result.done():
            return
        if error is None:
            result.set_result(value)
        else:
            result.set_exception(error)

    def read():
        try:
            value, error = function(), None
        except Exception as exception:
            value, error = None, exception
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            # The application may have exited while a network read was blocked.
            pass

    threading.Thread(target=read, name="wavrun-network-read", daemon=True).start()
    try:
        return await result
    except asyncio.CancelledError:
        if cancel_event is not None:
            cancel_event.set()
        raise
