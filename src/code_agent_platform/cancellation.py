"""Interrupt provider I/O without abandoning a live background model thread."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Lock, Thread

from langchain_openai import ChatOpenAI

_invocation = ContextVar('platform_model_invocation', default=None)


class ModelIO:
    """One lazy async I/O loop per platform, shared by sync agent workers."""
    def __init__(self):
        self.loop = None
        self.thread = None
        self.lock = Lock()

    def invoke(self, factory, check):
        check()
        with self.lock:
            if self.loop is None:
                ready = Event()
                def worker():
                    self.loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self.loop)
                    ready.set()
                    self.loop.run_forever()
                    self.loop.close()
                self.thread = Thread(target=worker, name='model-io', daemon=True)
                self.thread.start()
                ready.wait()
        finished = Event()
        async def request():
            try:
                return await factory()
            finally:
                finished.set()
        future = asyncio.run_coroutine_threadsafe(request(), self.loop)
        try:
            while True:
                try:
                    result = future.result(timeout=0.2)
                    check()
                    return result
                except FutureTimeout:
                    # A timeout raised by the provider is a real error, not a
                    # polling timeout. Never swallow it or spin on a done future.
                    if future.done():
                        result = future.result()
                        check()
                        return result
                    check()
        except BaseException:
            if not future.done():
                future.cancel()
                finished.wait(2)
            raise

    def close(self):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)


@contextmanager
def model_cancel_scope(runtime, check):
    token = _invocation.set((runtime, check))
    try:
        yield
    finally:
        _invocation.reset(token)


class CancellableModelMixin:
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        invocation = _invocation.get()
        if invocation is None:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        runtime, check = invocation
        return runtime.invoke(lambda: self._agenerate(messages, stop=stop, **kwargs), check)


class CancellableChatOpenAI(CancellableModelMixin, ChatOpenAI):
    pass
