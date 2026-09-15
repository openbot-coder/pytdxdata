"""健康分引擎：失败乘性×0.5 / 成功加性+0.2 / 连续失败冷却 / EWMA延迟"""
from __future__ import annotations

import time
from collections import defaultdict


class HealthEngine:
    """服务器健康状态管理（语义参照 easy-tdx 已验证实现）。"""

    COOLDOWN_SECONDS = 120.0
    FAIL_PENALTY = 0.5
    SUCCESS_BONUS = 0.2
    FAIL_STREAK_COOLDOWN = 1  # 任意一次通信失败即冷却，避免坏服务器被反复选中
    EWMA_ALPHA = 0.3

    def __init__(self) -> None:
        self._score: dict[str, float] = defaultdict(lambda: 1.0)
        self._fail_streak: dict[str, int] = defaultdict(int)
        self._cooldown_until: dict[str, float] = {}
        self._latency: dict[str, float] = {}  # EWMA延迟(秒)

    def record_success(self, server: str, latency: float | None = None) -> None:
        self._score[server] = min(1.0, self._score[server] + self.SUCCESS_BONUS)
        self._fail_streak[server] = 0
        self._cooldown_until.pop(server, None)
        if latency is not None:
            self.observe_latency(server, latency)

    def record_failure(self, server: str) -> None:
        self._score[server] = max(0.05, self._score[server] * self.FAIL_PENALTY)
        self._fail_streak[server] += 1
        if self._fail_streak[server] >= self.FAIL_STREAK_COOLDOWN:
            self._cooldown_until[server] = time.monotonic() + self.COOLDOWN_SECONDS

    def observe_latency(self, server: str, latency: float) -> None:
        old = self._latency.get(server)
        self._latency[server] = (
            latency if old is None else self.EWMA_ALPHA * latency + (1 - self.EWMA_ALPHA) * old
        )

    def score(self, server: str) -> float:
        return self._score.get(server, 1.0)

    def latency(self, server: str) -> float:
        return self._latency.get(server, float("inf"))

    def in_cooldown(self, server: str) -> bool:
        return time.monotonic() < self._cooldown_until.get(server, 0.0)

    def effective_latency(self, server: str) -> float:
        """排序键 = 延迟/健康分（越高越差）。"""
        return self.latency(server) / self.score(server)

    def rank(self, candidates: list[str]) -> list[str]:
        """按 effective_latency 升序排序，冷却中的服务器排最后。"""
        ok = [c for c in candidates if not self.in_cooldown(c)]
        bad = [c for c in candidates if self.in_cooldown(c)]
        ok.sort(key=self.effective_latency)
        bad.sort(key=self.effective_latency)
        return ok + bad
