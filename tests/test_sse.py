"""with_heartbeat 心跳机制的回归测试。

覆盖：
- 事件转发与流结束
- 空闲超时注入 HEARTBEAT 哨兵
- 消费者提前断开时后台任务的清理
"""
import asyncio

from atomcode_proxy.sse import HEARTBEAT, with_heartbeat


async def _events(*items):
    for item in items:
        await asyncio.sleep(0)
        yield item


async def _silent(duration):
    """静默一段时间后结束的流：期间不产事件，用于验证心跳注入。"""
    await asyncio.sleep(duration)
    if False:
        yield None


def test_with_heartbeat_forwards_events_and_ends_with_stream():
    async def run():
        out = []
        async for ev in with_heartbeat(_events("a", "b"), interval=1.0):
            out.append(ev)
        assert out == ["a", "b"]

    asyncio.run(run())


def test_with_heartbeat_injects_heartbeat_when_idle():
    async def run():
        out = []
        async for ev in with_heartbeat(_silent(0.3), interval=0.05):
            out.append(ev)
            if len(out) >= 2:
                break
        # 流存活但静默期间应持续注入心跳哨兵
        assert out == [HEARTBEAT, HEARTBEAT]

    asyncio.run(run())


def test_with_heartbeat_cleanup_on_early_close():
    """消费者提前断开（break -> generator aclose）时不能遗留挂起任务。"""
    async def run():
        gen = with_heartbeat(_silent(0.3), interval=0.01)
        out = []
        async for ev in gen:
            out.append(ev)
            if len(out) >= 1:
                await gen.aclose()
                break
        assert out == [HEARTBEAT]

    asyncio.run(run())
