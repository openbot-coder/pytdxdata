"""财务快照命令（标准通道 0x1F1876，35字段固定struct）"""
from __future__ import annotations

import struct

from ..._binary import slice_bytes, unpack_from
from ...models import FinanceRecord
from .base import BaseCommand

_FIN_FMT = "<fHHII" + "f" * 30
_FIN_SIZE = struct.calcsize(_FIN_FMT)
_SCALE = 10000.0  # 单位：万元/万股


class GetFinanceInfoCmd(BaseCommand[FinanceRecord | None]):
    """获取单只股票最新财务数据。"""

    def __init__(self, market: int, code: str) -> None:
        self.market = market
        self.code = code.encode("utf-8")

    def build_request(self) -> bytes:
        header = bytes.fromhex("0c1f187600010b000b0010000100".replace(" ", ""))
        return header + struct.pack("<B6s", self.market, self.code)

    def parse_response(self, body: bytes) -> FinanceRecord | None:
        pos = 2
        market_b, code_b = unpack_from("<B6s", body, pos, "finance header")
        pos += 7
        if pos + _FIN_SIZE > len(body):
            return None
        fields = struct.unpack(_FIN_FMT, slice_bytes(body, pos, _FIN_SIZE, "finance body"))
        (
            liutong_guben, province, industry, updated_date, ipo_date,
            zong_guben, guojia_gu, faqiren_faren_gu, faren_gu, b_gu,
            h_gu, zhigong_gu, zong_zichan, liudong_zichan, guding_zichan,
            wuxing_zichan, gudong_renshu, liudong_fuzhai, changqi_fuzhai, ziben_gongjijin,
            jing_zichan, zhuying_shouru, zhuying_lirun, yingshou_zhangkuan, yingye_lirun,
            touzi_shouyu, jingying_xianjinliu, zong_xianjinliu, cunhuo, lirun_zonghe,
            shuihou_lirun, jing_lirun, weifen_lirun, meigujing_zichan, _reserve2,
        ) = fields
        return FinanceRecord(
            market=market_b, code=code_b.decode("utf-8").rstrip("\x00"),
            liutong_guben=liutong_guben * _SCALE, zong_guben=zong_guben * _SCALE,
            guojia_gu=guojia_gu * _SCALE, faqiren_faren_gu=faqiren_faren_gu * _SCALE,
            faren_gu=faren_gu * _SCALE, b_gu=b_gu * _SCALE, h_gu=h_gu * _SCALE,
            zhigong_gu=zhigong_gu * _SCALE,
            province=int(province), industry=int(industry),
            updated_date=int(updated_date), ipo_date=int(ipo_date),
            gudong_renshu=gudong_renshu,
            zong_zichan=zong_zichan * _SCALE, liudong_zichan=liudong_zichan * _SCALE,
            guding_zichan=guding_zichan * _SCALE, wuxing_zichan=wuxing_zichan * _SCALE,
            liudong_fuzhai=liudong_fuzhai * _SCALE, changqi_fuzhai=changqi_fuzhai * _SCALE,
            ziben_gongjijin=ziben_gongjijin * _SCALE,
            jing_zichan=jing_zichan * _SCALE, zhuying_shouru=zhuying_shouru * _SCALE,
            zhuying_lirun=zhuying_lirun * _SCALE, yingshou_zhangkuan=yingshou_zhangkuan * _SCALE,
            yingye_lirun=yingye_lirun * _SCALE, touzi_shouyu=touzi_shouyu * _SCALE,
            jingying_xianjinliu=jingying_xianjinliu * _SCALE,
            zong_xianjinliu=zong_xianjinliu * _SCALE,
            cunhuo=cunhuo * _SCALE, lirun_zonghe=lirun_zonghe * _SCALE,
            shuihou_lirun=shuihou_lirun * _SCALE, jing_lirun=jing_lirun * _SCALE,
            weifen_lirun=weifen_lirun * _SCALE, meigujing_zichan=meigujing_zichan,
            reserve2=_reserve2,
        )
