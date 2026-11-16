"""
多版本备份仓库（对应 Java 版 BackupStore）。

早期实现是「改之前复制一份 文件名.bak」，问题很直接：
连着改两次，第一版的原始内容就永久丢了 —— 而「连改两次」恰恰是最常见的场景。

这里改成集中式快照仓库，三个好处：
    1. 每个文件保留最近 20 个版本，可以一层层往回退；
    2. 快照统一放在项目下的 .mycode/backups/，不污染源码目录
       （以前 .bak 会和源码挤在一起，还容易被误提交）；
    3. 快照路径里带着原文件的相对路径，所以「恢复」不需要额外记录映射关系。

目录结构：
    .mycode/backups/samples/InvoiceService.py/20261115-165812-123.bak
                     └────── 原始相对路径 ──────┘ └── 时间戳 ──┘
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 每个文件最多保留的版本数
KEEP_PER_FILE = 20

# 快照文件后缀
SUFFIX = ".bak"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """快照条目，仅用于展示。"""

    file: Path     # 恢复到哪个文件
    stamp: str     # 备份时间（取自快照文件名）
    size: int      # 快照大小（字节）


class BackupStore:
    """集中式快照仓库。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._backup_root = self._root / ".mycode" / "backups"
        # 时间戳在毫秒内可能撞名（自动化的连续编辑很常见），加锁兜序号
        self._lock = threading.Lock()

    def save(self, file: Path) -> Path:
        """保存一份快照（在写入新内容之前调用）。返回快照文件路径。"""
        source = Path(file).resolve()
        if not source.is_relative_to(self._root):
            raise ValueError("拒绝备份项目目录之外的文件: " + str(file))
        raw = source.read_bytes()

        # 用原文件的相对路径当子目录，这样从快照路径就能反推出原文件位置
        rel_dir = source.relative_to(self._root)
        target_dir = self._backup_root / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            # 只取一次 now()：调两次可能跨秒边界，秒段与毫秒段对不上
            now = datetime.now()
            stamp = now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}"
            target = target_dir / (stamp + SUFFIX)
            seq = 1
            while target.exists():
                # 同一毫秒内连续两次修改会撞名，加序号兜底
                target = target_dir / f"{stamp}-{seq}{SUFFIX}"
                seq += 1
            target.write_bytes(raw)  # 变量路径写盘一律走 pathlib（见 tools 层约定）
            self._prune(target_dir)
        return target

    def restore(self, file: Path | None = None) -> Path:
        """把某个文件恢复到最新的快照，并消耗掉这个快照（这样连续 undo 能一退再退）。

        file 传 None 表示「全局最近一次修改过的文件」。
        没有可用快照时抛 RuntimeError。
        """
        snapshot = self.newest(file)
        if snapshot is None:
            raise RuntimeError("没有可用的历史版本")
        target = self._original_of(snapshot)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(snapshot.read_bytes())
        snapshot.unlink(missing_ok=True)
        return target

    def newest(self, file: Path | None = None) -> Path | None:
        """找出最新的快照；file 限定单文件，None 表示在整个仓库里找最近一个。"""
        if not self._backup_root.is_dir():
            return None
        if file is not None:
            rel_dir = Path(file).resolve().relative_to(self._root)
            snapshots = self._list_snapshots(self._backup_root / rel_dir)
            return snapshots[0] if snapshots else None
        # 全局模式：文件名是固定宽度的「日期-时间-毫秒」，直接按路径倒序就是时间倒序
        all_snapshots: list[Path] = []
        for p in self._backup_root.rglob("*" + SUFFIX):
            if p.is_file():
                all_snapshots.append(p)
        return max(all_snapshots, key=lambda p: p.name) if all_snapshots else None

    def recent(self, limit: int) -> list[Snapshot]:
        """全局最近若干条快照，用于 /history 展示。

        每条包含：恢复到哪个文件、备份时间、大小。
        """
        if not self._backup_root.is_dir():
            return []
        all_snapshots = [p for p in self._backup_root.rglob("*" + SUFFIX) if p.is_file()]
        all_snapshots.sort(key=lambda p: p.name, reverse=True)

        out: list[Snapshot] = []
        for p in all_snapshots:
            if len(out) >= limit:
                break
            name = p.name
            out.append(Snapshot(self._original_of(p), name[:min(17, len(name))], p.stat().st_size))
        return out

    # ---------------- 内部工具 ----------------

    def _list_snapshots(self, directory: Path) -> list[Path]:
        """列出目录下的快照，按文件名倒序（文件名即时间序）。"""
        if not directory.is_dir():
            return []
        return sorted((p for p in directory.iterdir() if p.is_file() and p.name.endswith(SUFFIX)),
                      key=lambda p: p.name, reverse=True)

    def _original_of(self, snapshot: Path) -> Path:
        """从快照路径反推原始文件路径：去掉 backupRoot 前缀和最后一段文件名。"""
        relative_dir = snapshot.parent.relative_to(self._backup_root)
        return (self._root / relative_dir).resolve()

    def _prune(self, directory: Path) -> None:
        """只保留最近 KEEP_PER_FILE 个版本，避免仓库无限膨胀。"""
        snapshots = self._list_snapshots(directory)
        for old in snapshots[KEEP_PER_FILE:]:
            old.unlink(missing_ok=True)
