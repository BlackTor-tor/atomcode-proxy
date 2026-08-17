"""SSE 流式辅助：在事件间隙注入心跳，防止上游客户端空闲超时断开。

Cursor / Codex CLI / Claude Code 等客户端通常有 1~5 分钟空闲超时；
模型长时间思考或工具执行期间 daemon 可能数分钟无事件输出，
若代理不发送任何字节，上游连接会被客户端掐断，表现为"任务中断"。
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

# 心跳哨兵：with_heartbeat 在超过 interval 无事件时 yield 它，
# 由调用方转换为具体的 SSE 心跳帧（OpenAI 用注释行 ": ping"，Anthropic 用 ping 事件）。
HEARTBEAT: Any = object()

# 心跳间隔：15s 足以压住大多数客户端 1~5 分钟的空闲超时
HEARTBEAT_INTERVAL = 15.0


async def with_heartbeat(
    events: AsyncIterator[Any],
    interval: float = HEARTBEAT_INTERVAL,
) -> AsyncIterator[Any]:
    """转发事件流；间隔 interval 秒无事件时注入 HEARTBEAT 哨兵。

    事件到达时立即转发；daemon 静默超过 interval 秒时 yield HEARTBEAT，
    调用方据此发出心跳帧。事件流结束（StopAsyncIteration）时正常返回。
    """
    events_it = events.__aiter__()
    events_task = asyncio.ensure_future(events_it.__anext__())
    beat_task = asyncio.ensure_future(asyncio.sleep(interval))
    try:
        while True:
            done, _ = await asyncio.wait(
                {events_task, beat_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if events_task in done:
                try:
                    ev = events_task.result()
                except StopAsyncIteration:
                    return
                yield ev
                events_task = asyncio.ensure_future(events_it.__anext__())
            if beat_task in done:
                yield HEARTBEAT
                beat_task = asyncio.ensure_future(asyncio.sleep(interval))
    finally:
        events_task.cancel()
        beat_task.cancel()
        # 等待被取消的 task 落地：若 events_task 此刻恰以业务异常完成（如 daemon
        # 断连），不 await 会造成 "exception was never retrieved" 告警。
        # 若当前协程自身被取消，CancelledError 从 gather 正常向外传播。
        await asyncio.gather(events_task, beat_task, return_exceptions=True)
