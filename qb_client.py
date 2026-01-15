import os
import time
from qbittorrentapi import Client
from logger import logger

class QBittorrentClient:
    """
    使用 qbittorrent-api 实现的精简版客户端，添加了种子状态检查功能。
    """

    def __init__(
        self,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        """
        初始化客户端，自动从环境变量中加载配置。
        """
        self.host = url
        self.username = username
        self.password = password

        try:
            self.client = Client(host=self.host, username=self.username, password=self.password, VERIFY_WEBUI_CERTIFICATE=False)
            self.client.auth_log_in()
            logger.info(f"成功连接到 qBittorrent API: {self.host}")
        except Exception as e:
            raise ConnectionError(f"连接 qBittorrent API 失败: {e}")

    def is_alive(self) -> bool:
        """简易健康检查：尝试请求应用版本。"""
        try:
            ver = self.client.app.version
            logger.info(f"{self.host} app version is {ver}")
            return True
        except Exception:
            return False

    def stop_report_delete_all_except_categories(
        self,
        exclude_categories,
        *,
        delete_files: bool = True,
        wait_seconds: int = 5,
    ) -> int:
        """
        除 exclude_categories 外：仅对“正在下载”的任务强制汇报 + 暂停（不再删除）。
        :param exclude_categories: 要保留(不处理)的分类名，支持 str 或 list/set/tuple[str]
        :param delete_files: 已废弃参数，保留仅为兼容
        :param wait_seconds: 强制汇报后等待秒数（给 tracker 一点时间）
        :return: 实际处理（暂停）的任务数量（仅下载中）
        """
        if isinstance(exclude_categories, str):
            exclude_set = {exclude_categories}
        else:
            exclude_set = set(exclude_categories or [])

        # 兼容 qbittorrent-api 不同版本的调用方式
        if hasattr(self.client, "torrents_info"):
            torrents = self.client.torrents_info()
        else:
            torrents = self.client.torrents.info()

        # 过滤：category 在 exclude_set 的不动，其它全处理；
        # 仅挑选“下载中/等待下载相关”状态
        download_like_states = {
            "downloading",     # 正在下载
            "stalledDL",       # 下载停滞
            "queuedDL",        # 等待下载队列
            "metaDL",          # 元数据下载中
            "checkingDL",      # 下载前校验中
            "allocating",      # 为下载分配空间
        }
        hashes = []
        for t in torrents:
            h = getattr(t, "hash", None)
            cat = getattr(t, "category", None)
            state = getattr(t, "state", None)
            state_str = state if isinstance(state, str) else (str(state) if state is not None else None)
            if not h:
                continue
            if cat in exclude_set:
                continue
            if state_str not in download_like_states:
                continue
            hashes.append(h)

        if not hashes:
            logger.info(f"未找到可处理任务（排除分类: {sorted(exclude_set)}）。")
            return 0

        hash_str = "|".join(hashes)

        # 1) 强制汇报
        try:
            self.client.torrents_reannounce(torrent_hashes=hash_str)
            logger.info(
                f"已对 {len(hashes)} 个下载/等待下载任务发出强制汇报指令（排除分类: {sorted(exclude_set)}）。"
            )
        except Exception as e:
            logger.warning(f"强制汇报失败（仍继续后续操作）：{e}")

        # 给 tracker 一点时间（可调）
        if wait_seconds and wait_seconds > 0:
            time.sleep(wait_seconds)

        # 2) 暂停（stop）
        try:
            self.client.torrents_stop(torrent_hashes=hash_str)
            logger.info(
                f"已对 {len(hashes)} 个下载/等待下载任务发出暂停指令（排除分类: {sorted(exclude_set)}），未执行删除。"
            )
        except Exception as e:
            logger.warning(f"暂停失败（仍继续后续操作）：{e}")
        return len(hashes)


    def pause_all(self):
        """
        仅暂停处于下载/等待下载状态的种子任务。
        """
        # 兼容不同版本的调用方式
        if hasattr(self.client, "torrents_info"):
            torrents = self.client.torrents_info()
        else:
            torrents = self.client.torrents.info()

        download_like_states = {
            "downloading",
            "stalledDL",
            "queuedDL",
            "metaDL",
            "checkingDL",
            "allocating",
        }
        hashes = []
        for t in torrents:
            h = getattr(t, "hash", None)
            state = getattr(t, "state", None)
            state_str = state if isinstance(state, str) else (str(state) if state is not None else None)
            if not h:
                continue
            if state_str in download_like_states:
                hashes.append(h)

        if not hashes:
            logger.info("没有处于下载/等待下载状态的任务需要暂停。")
            return

        hash_str = "|".join(hashes)
        self.client.torrents_reannounce(torrent_hashes=hash_str)
        self.client.torrents_stop(torrent_hashes=hash_str)

        logger.info(f"已发出强制汇报&暂停 {len(hashes)} 个下载/等待下载任务。")
            
    def delete_all(self, *, delete_files: bool = False) -> None:
        """
        删除全部任务。
        :param delete_files: True 时会连同本地数据一并删除（危险操作）
        """
        self.client.torrents.delete(hashes="all", delete_files=delete_files)
        logger.info("已发出删除全部任务的指令。")

    def resume_all(self):
        """
        启动（恢复）所有“处于暂停状态”的种子任务，不进行强制汇报。
        """
        # 获取当前任务列表
        if hasattr(self.client, "torrents_info"):
            torrents = self.client.torrents_info()
        else:
            torrents = self.client.torrents.info()

        paused_states = {
                "pausedDL",
                "pausedUP",
                "paused",
                "stopped",
                "stoppedDL",
                "stoppedUP",
            }
        hashes = []
        for t in torrents:
            h = getattr(t, "hash", None)
            state = getattr(t, "state", None)
            state_str = state if isinstance(state, str) else (str(state) if state is not None else None)
            if not h:
                continue
            if state_str in paused_states:
                hashes.append(h)

        if not hashes:
            logger.info("没有处于暂停状态的任务需要恢复。")
            return

        hash_str = "|".join(hashes)
        self.client.torrents_resume(torrent_hashes=hash_str)
        logger.info(f"已发出恢复 {len(hashes)} 个处于暂停状态的任务的指令。")

## 测试代码
#if __name__ == "__main__":
#    # 初始化客户端
#    qb_client = QBittorrentClient("185.244.194.39")
#    
#    qb_client.is_alive()
#    
#    qb_client.exit_block_new()

