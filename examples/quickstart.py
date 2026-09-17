"""pytdxdata 快速上手：报价 / K线 / 分时 / 板块汇总。

运行：
    python examples/quickstart.py

说明：数据来自通达信公开行情服务器（无需账号，15 分钟延时）。
标的一律用字符串（``sh600000`` / ``sz000001`` / ``hk00700`` / ``usAAPL`` / ``cffex:IFL0``）。
"""

import asyncio

from pytdxdata import TdxData


async def main() -> None:
    # TdxData 为异步上下文管理器：进入时建连，退出时释放
    async with TdxData() as td:
        # 1) 批量五档报价（可一次传多只，超过 80 只自动切批）
        quotes = await td.get_quotes(["sh600000", "sz000001"])
        print("== 报价 ==")
        for q in quotes:
            print(
                f"{q.code}  现价={q.price}  昨收={q.pre_close}  "
                f"买一={q.bid[0]}  卖一={q.ask[0]}  成交量={q.vol}"
            )

        # 2) 日K线（最近 10 根）
        bars = await td.get_bars("sh600000", count=10)
        print("\n== 日K（最近 3 根）==")
        for b in bars[-3:]:
            print(
                f"{b.datetime:%Y-%m-%d}  "
                f"开={b.open} 高={b.high} 低={b.low} 收={b.close} 量={b.vol}"
            )

        # 3) 当日分时
        minutes = await td.get_minutes("sh600000")
        latest = minutes[-1].price if minutes else "-"
        print(f"\n== 分时 ==\n共 {len(minutes)} 个点，最新价={latest}")

        # 4) 板块汇总（示例：通达信行业板块 881001）
        summary = await td.get_board_summary("881001")
        print("\n== 板块汇总 881001 ==")
        for key, value in summary.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
