"""CLI 服务器文件命令测试：block / industry / financial-list / financial。

全部离线：用 FakeTD 提供异步桩，不发任何网络请求。
"""
from __future__ import annotations

from pytdxdata.cli import cmd_block, cmd_financial, cmd_financial_list, cmd_industry
from pytdxdata.models import FinancialFileInfo, FinancialRecord, IndustryInfo, TdxBlock

_BLOCKS = [
    TdxBlock(name="人工智能", category=2, count=3, codes=["300308", "002415", "688111"]),
    TdxBlock(name="白酒", category=0, count=1, codes=["600519"]),
]
_INDUSTRY = {
    "600000": IndustryInfo(code="600000", tdx_industry="880305", sw_industry="801780"),
    "000001": IndustryInfo(code="000001", tdx_industry="880471", sw_industry=""),
}
_FILES = [FinancialFileInfo(filename="gpcw20260331.zip", hash="abc123", filesize=1234567)]
_RECORDS = [
    FinancialRecord(code="000001", market=0, report_date=20260331, fields=[1.0, 2.5, 3.5, 4.0]),
    FinancialRecord(code="600000", market=1, report_date=20260331, fields=[9.0, 8.0]),
]


class FakeTD:
    """最小异步桩：只实现 4 个服务器文件方法。"""

    def __init__(self, blocks=None, industry=None, files=None, records=None):
        self.blocks = _BLOCKS if blocks is None else blocks
        self.industry = _INDUSTRY if industry is None else industry
        self.files = _FILES if files is None else files
        self.records = _RECORDS if records is None else records

    async def get_block_parsed(self, filename):
        return self.blocks

    async def get_industry_map(self):
        return self.industry

    async def get_financial_file_infos(self):
        return self.files

    async def get_financial_records_parsed(self, filename):
        return self.records


# --------------------------------------------------------------------------- #
# block
# --------------------------------------------------------------------------- #

async def test_cmd_block_basic(capsys):
    await cmd_block(FakeTD(), "block_gn.dat", 30, False)
    out = capsys.readouterr().out
    assert "block_gn.dat 板块文件 (2 个板块)" in out
    assert "概念" in out and "人工智能" in out       # category=2 → 概念
    assert "行业" in out and "白酒" in out           # category=0 → 行业
    assert "300308" in out
    assert "其余" not in out


async def test_cmd_block_limit_and_codes(capsys):
    await cmd_block(FakeTD(), "block_gn.dat", 1, True)
    out = capsys.readouterr().out
    assert "人工智能" in out
    assert "白酒" not in out                          # 被 --limit 1 截掉
    assert "其余 1 个板块省略" in out
    assert "300308 002415 688111" in out             # --codes 展开完整成分


async def test_cmd_block_unknown_category_and_empty(capsys):
    weird = [TdxBlock(name="神秘", category=9, count=0, codes=[])]
    await cmd_block(FakeTD(blocks=weird), "block_x.dat", 30, False)
    assert "?" in capsys.readouterr().out

    await cmd_block(FakeTD(blocks=[]), "block_gn.dat", 30, False)
    assert "0 个板块" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# industry
# --------------------------------------------------------------------------- #

async def test_cmd_industry_all(capsys):
    await cmd_industry(FakeTD(), [], 30, None)
    out = capsys.readouterr().out
    assert "行业分类 tdxhy.cfg (共 2 只)" in out
    assert "600000" in out and "880305" in out and "801780" in out
    assert "880471" in out and "-" in out             # 申万缺失 → "-"


async def test_cmd_industry_codes_and_missing(capsys):
    await cmd_industry(FakeTD(), ["600000", "999999"], 30, None)
    out = capsys.readouterr().out
    assert "600000" in out and "通达信=880305" in out
    assert "999999" in out and "未收录" in out


async def test_cmd_industry_search_and_limit(capsys):
    await cmd_industry(FakeTD(), [], 30, "880305")
    out = capsys.readouterr().out
    assert "匹配 '880305' 1 条" in out
    assert "600000" in out and "000001" not in out

    await cmd_industry(FakeTD(), [], 1, None)
    assert "其余 1 条省略" in capsys.readouterr().out

    await cmd_industry(FakeTD(industry={}), [], 30, None)
    assert "共 0 只" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# financial-list
# --------------------------------------------------------------------------- #

async def test_cmd_financial_list(capsys):
    await cmd_financial_list(FakeTD(), 30)
    out = capsys.readouterr().out
    assert "gpcw.txt, 共 1 个" in out
    assert "gpcw20260331.zip" in out and "abc123" in out and "1234567" in out
    assert "tdx financial gpcw20260331.zip" in out


async def test_cmd_financial_list_limit_and_empty(capsys):
    extra = _FILES + [FinancialFileInfo(filename="gpcw20251231.zip", hash="def", filesize=1)]
    await cmd_financial_list(FakeTD(files=extra), 1)
    assert "其余 1 个省略" in capsys.readouterr().out

    await cmd_financial_list(FakeTD(files=[]), 30)
    assert "共 0 个" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# financial
# --------------------------------------------------------------------------- #

async def test_cmd_financial_basic(capsys):
    await cmd_financial(FakeTD(), "gpcw20260331.zip", None, 5, 4)
    out = capsys.readouterr().out
    assert "gpcw20260331.zip 财报记录 (2 条)" in out
    assert "市场=深" in out and "市场=沪" in out
    assert "报告期=20260331" in out
    assert "1 2.5 3.5 4" in out


async def test_cmd_financial_filter_and_fields_zero(capsys):
    await cmd_financial(FakeTD(), "gpcw20260331.zip", "600000", 5, 0)
    out = capsys.readouterr().out
    assert "(1 条)" in out
    assert "600000" in out and "000001" not in out
    assert "字段数=2" in out


async def test_cmd_financial_limit_and_empty(capsys):
    await cmd_financial(FakeTD(), "gpcw20260331.zip", None, 1, 4)
    assert "其余 1 条省略" in capsys.readouterr().out

    await cmd_financial(FakeTD(records=[]), "gpcw20260331.zip", None, 5, 4)
    assert "(0 条)" in capsys.readouterr().out
