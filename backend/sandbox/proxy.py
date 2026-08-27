"""沙箱白名单代理（P2 可选联网）：HTTP CONNECT 代理 + 域名白名单。

设计（面试可讲）：
  - 为什么「可选联网」：默认断网是安全卖点，但部分任务（调 API / 下载数据）确实需要网络；
  - 方案：沙箱容器接入受限网络，所有出站走**本代理**，代理按域名白名单放行
    （`PROXY_WHITELIST_DOMAINS`），非白名单一律拒绝（403）——「功能可用但受限」；
  - 诚实边界：这是**应用层白名单**（配合可信任务/代码）；强制白名单需网络层防火墙
    （iptables / 服务网格 / 独立沙箱网络），生产环境应叠加。

启动（面试演示时）：
    python -m sandbox.proxy        # 默认端口 PROXY_PORT(8888)
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

PORT = int(os.getenv("PROXY_PORT", "8888"))
WHITELIST = {
    d.strip().lower()
    for d in os.getenv(
        "PROXY_WHITELIST_DOMAINS",
        "api.deepseek.com,pypi.org,github.com",
    ).split(",")
    if d.strip()
}


def is_allowed(host: str) -> bool:
    """域名白名单匹配（精确或子域）。"""
    host = (host or "").split(":")[0].strip().lower()
    return any(host == d or host.endswith("." + d) for d in WHITELIST)


async def _pipe(reader, writer):
    """双向转发一段数据流。"""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(reader, writer):
    peer = writer.get_extra_info("peername")
    try:
        line = await reader.readline()
        if not line:
            return
        parts = line.decode(errors="replace").split()
        if len(parts) < 2:
            return
        method, target = parts[0], parts[1]
        # 消费剩余请求头
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break

        if method == "CONNECT":  # HTTPS 隧道
            host, _, port = target.rpartition(":")
            if not is_allowed(host):
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await writer.drain()
                logger.warning("代理拒绝非白名单域名: %s (%s)", target, peer)
                return
            try:
                up_r, up_w = await asyncio.open_connection(host, int(port or 443))
            except Exception:  # noqa: BLE001
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
        else:  # http:// 明文（少数）——直接拒绝，只支持 CONNECT，简化攻击面
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
    except Exception as e:  # noqa: BLE001
        logger.debug("代理连接异常: %s", e)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def main():
    server = await asyncio.start_server(_handle, "0.0.0.0", PORT)
    logger.info(
        "白名单代理已启动: 端口 %s，白名单 %s",
        PORT,
        sorted(WHITELIST),
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(main())
