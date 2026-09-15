"""服务器文件下载 + 解析：板块 / 行业分类 / 历史专业财报。

运行：
    python examples/server_files.py

这组接口把通达信服务器上的二进制文件（block_*.dat / tdxhy.cfg / gpcw*.zip）
直接下载并解析为结构化数据（解析器位于 pytdxdata.codec，纯标准库、可脱网单测）。
"""

import asyncio

from pytdxdata import TdxData


async def main() -> None:
    async with TdxData() as td:
        # 1) 板块文件 -> 板块成分
        #    block_zs.dat=行业 / block_gn.dat=概念 / block_fg.dat=风格
        blocks = await td.get_block_parsed("block_gn.dat")
        print(f"== 概念板块（共 {len(blocks)} 个）==")
        for blk in blocks[:5]:
            print(f"  [{blk.category}] {blk.name}  成分数={blk.count}  示例={blk.codes[:5]}")

        # 2) 行业分类（tdxhy.cfg）-> {6位代码: 通达信行业 + 申万行业}
        industry = await td.get_industry_map()
        print(f"\n== 行业分类（共 {len(industry)} 只）==")
        for code in ("600000", "000001", "300750"):
            info = industry.get(code)
            if info is not None:
                sw = info.sw_industry or "-"
                print(f"  {code}  通达信={info.tdx_industry}  申万={sw}")

        # 3) 历史专业财报文件索引（tdxfin/gpcw.txt）
        infos = await td.get_financial_file_infos()
        print(f"\n== 财报文件索引（共 {len(infos)} 个）==")
        for fi in infos[-3:]:
            print(f"  {fi.filename}  md5={fi.hash[:12]}...  {fi.filesize} 字节")

        # 4) 下载并解析最新一期财报（gpcw*.zip -> 每股原始 float 字段）
        if infos:
            latest = infos[-1].filename
            records = await td.get_financial_records_parsed(latest)
            print(f"\n== {latest} 财报记录（共 {len(records)} 条）==")
            for r in records[:5]:
                market = "沪" if r.market == 1 else "深"
                print(f"  {r.code}  市场={market}  报告期={r.report_date}  字段数={len(r.fields)}")


if __name__ == "__main__":
    asyncio.run(main())
