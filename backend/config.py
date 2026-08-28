"""
微信解析助手 - Configuration
自动检测微信数据路径和相关配置，支持多账号
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


SMOKE_TEST_ENV = "WECHAT_ASSISTANT_SMOKE_TEST"


def is_smoke_test_environment(environ=None) -> bool:
    """Return whether release smoke-test isolation is explicitly enabled."""

    source = os.environ if environ is None else environ
    return str(source.get(SMOKE_TEST_ENV, "") or "").strip() == "1"


@dataclass
class WeChatAccount:
    """单个微信账号信息"""
    wxid: str
    wx_root: Path
    msg_dir: Optional[Path] = None
    micromsg_db: Optional[Path] = None
    display_name: Optional[str] = None
    alias: Optional[str] = None

    @property
    def sns_db(self) -> Optional[Path]:
        """Return the verified WeChat 4.x Moments database location."""
        if self.wx_root.name.lower() != "xwechat_files" or not self.msg_dir:
            return None
        candidate = self.msg_dir.parent / "sns" / "sns.db"
        try:
            return (
                candidate
                if candidate.is_file() and candidate.stat().st_size > 0
                else None
            )
        except OSError:
            return None

    @property
    def head_image_db(self) -> Optional[Path]:
        """Return the WeChat 4.x local avatar database when available."""
        if self.wx_root.name.lower() != "xwechat_files" or not self.msg_dir:
            return None
        candidate = self.msg_dir.parent / "head_image" / "head_image.db"
        try:
            return (
                candidate
                if candidate.is_file() and candidate.stat().st_size > 0
                else None
            )
        except OSError:
            return None

    @property
    def last_active(self) -> str:
        """最后活跃时间 (根据数据库修改时间)"""
        if not self.msg_dir or not self.msg_dir.exists():
            return ""
        try:
            from datetime import datetime
            latest = None
            for f in self.msg_dir.glob("message_*.db"):
                if f.is_file() and f.stat().st_size > 0:
                    mtime = f.stat().st_mtime
                    if latest is None or mtime > latest:
                        latest = mtime
            if latest:
                dt = datetime.fromtimestamp(latest)
                now = datetime.now()
                if dt.date() == now.date():
                    return f"今天 {dt.strftime('%H:%M')}"
                elif (now - dt).days == 1:
                    return f"昨天 {dt.strftime('%H:%M')}"
                elif (now - dt).days < 7:
                    return f"{(now-dt).days}天前"
                return dt.strftime("%m月%d日")
        except Exception:
            pass
        return ""

    @property
    def db_count(self) -> int:
        """消息数据库数量"""
        if not self.msg_dir or not self.msg_dir.exists():
            return 0
        try:
            return len([f for f in self.msg_dir.glob("message_*.db")
                       if f.is_file() and f.stat().st_size > 0 and "-" not in f.name])
        except Exception:
            return 0

    def to_dict(self) -> dict:
        dbs = []
        if self.msg_dir and self.msg_dir.exists():
            for f in sorted(self.msg_dir.glob("message_*.db")):
                if f.is_file() and f.stat().st_size > 0 and "-" not in f.name:
                    dbs.append(f.name)
        return {
            "wxid": self.wxid,
            "wx_root": str(self.wx_root),
            "msg_dir": str(self.msg_dir) if self.msg_dir else None,
            "display_name": self.display_name,
            "alias": self.alias,
            "msg_dbs": dbs,
            "last_active": self.last_active,
            "db_count": self.db_count,
            "supports_moments": self.sns_db is not None,
        }


def detect_all_accounts() -> list[WeChatAccount]:
    """扫描常见的微信 4.x 数据目录，找出所有登录过的账号。"""
    # Packaged release smoke tests must be deterministic and must never inspect
    # a maintainer's real WeChat folders. The desktop smoke harness enables this
    # flag only for its isolated child process.
    if is_smoke_test_environment():
        return []

    accounts: dict[str, WeChatAccount] = {}  # wxid -> account

    # 微信 4.x 的账号数据均位于 xwechat_files/<wxid_*>/db_storage。
    roots_to_scan: list[Path] = []

    def add_root(candidate: Path) -> None:
        try:
            if (
                candidate.name.lower() == "xwechat_files"
                and candidate.is_dir()
                and candidate not in roots_to_scan
            ):
                roots_to_scan.append(candidate)
        except OSError:
            pass

    for drive_letter in "CDEFG":
        add_root(Path(f"{drive_letter}:\\") / "xwechat_files")

    # 用户目录中的常见 4.x 数据位置。
    userprofile_value = os.environ.get("USERPROFILE", "").strip()
    if userprofile_value:
        userprofile = Path(userprofile_value)
        add_root(userprofile / "xwechat_files")
        add_root(userprofile / "Documents" / "xwechat_files")

    # 微信 4.x 会把用户选择的数据存储根目录写入本地 ini。读取它可以
    # 覆盖“自定义目录/xwechat_files”这类不在盘符根目录下的位置。
    appdata_value = os.environ.get("APPDATA", "").strip()
    if appdata_value:
        config_dir = Path(appdata_value) / "Tencent" / "xwechat" / "config"
        try:
            ini_files = list(config_dir.glob("*.ini")) if config_dir.is_dir() else []
        except OSError:
            ini_files = []
        for ini_file in ini_files:
            content = None
            for encoding in ("utf-8-sig", "gbk"):
                try:
                    content = ini_file.read_text(encoding=encoding)[:1024].strip()
                    break
                except UnicodeDecodeError:
                    continue
                except OSError:
                    break
            if not content or any(char in content for char in "\n\r\x00"):
                continue
            data_root = Path(os.path.expandvars(content)).expanduser()
            add_root(
                data_root
                if data_root.name.lower() == "xwechat_files"
                else data_root / "xwechat_files"
            )

    # 扫描各盘根目录的一级子目录，兼容用户自定义盘符位置。
    try:
        skip_dirs = {"Windows", "Program Files", "Program Files (x86)",
                     "ProgramData", "$Recycle.Bin", "System Volume Information",
                     "Recovery", "node_modules", ".git", "AppData", "LocalCache"}
        drives = [f"{d}:\\" for d in "CDEFG" if os.path.exists(f"{d}:\\")]
        for drive in drives:
            try:
                for item in os.scandir(drive):
                    if not item.is_dir() or item.name in skip_dirs:
                        continue
                    if item.name.lower() == "xwechat_files":
                        add_root(Path(item.path))
            except OSError:
                pass
    except Exception:
        pass

    # 从每个根目录提取账号
    for root in roots_to_scan:
        if not root or not root.exists():
            continue
        for item in root.iterdir():
            if not item.is_dir() or not item.name.startswith("wxid_"):
                continue
            db_storage = item / "db_storage"
            if not db_storage.exists():
                continue
            msg_dir = db_storage / "message"
            micromsg_db = db_storage / "contact" / "contact.db"

            wxid = item.name
            if wxid in accounts:
                continue
            accounts[wxid] = WeChatAccount(
                wxid=wxid,
                wx_root=root,
                msg_dir=msg_dir if msg_dir.exists() else None,
                micromsg_db=micromsg_db if micromsg_db.exists() else None,
            )

    return list(accounts.values())


@dataclass
class WeChatConfig:
    """微信相关配置"""

    # 所有检测到的账号
    accounts: list[WeChatAccount] = field(default_factory=list)
    # 当前选中的账号索引 (-1 = 未选中)
    active_index: int = -1
    # 解密密钥 (64位十六进制字符串)
    key: Optional[str] = None
    # 解密后的临时数据库
    decrypted_db: Optional[Path] = None
    # 用户自己的微信名和微信号 (当前账号)
    display_name: Optional[str] = None
    alias: Optional[str] = None

    @property
    def wxid(self) -> Optional[str]:
        """当前选中账号的 wxid"""
        if 0 <= self.active_index < len(self.accounts):
            return self.accounts[self.active_index].wxid
        return None

    @property
    def wx_root(self) -> Optional[Path]:
        """当前选中账号的数据根目录"""
        if 0 <= self.active_index < len(self.accounts):
            return self.accounts[self.active_index].wx_root
        return None

    @property
    def msg_dir(self) -> Optional[Path]:
        """当前选中账号的消息数据库目录"""
        if 0 <= self.active_index < len(self.accounts):
            return self.accounts[self.active_index].msg_dir
        return None

    @property
    def micromsg_db(self) -> Optional[Path]:
        """当前选中账号的联系人数据库路径"""
        if 0 <= self.active_index < len(self.accounts):
            return self.accounts[self.active_index].micromsg_db
        return None

    @property
    def sns_db(self) -> Optional[Path]:
        """朋友圈数据库路径；目前仅确认支持微信 4.x。"""
        if 0 <= self.active_index < len(self.accounts):
            return self.accounts[self.active_index].sns_db
        return None

    @property
    def head_image_db(self) -> Optional[Path]:
        """当前账号的微信 4.x 本地头像数据库。"""
        if 0 <= self.active_index < len(self.accounts):
            return self.accounts[self.active_index].head_image_db
        return None

    def detect_and_set_accounts(self) -> list[WeChatAccount]:
        """扫描并设置所有可用账号，自动选择第一个，加载缓存名称"""
        self.accounts = detect_all_accounts()
        self._load_cached_names()
        if self.accounts and self.active_index < 0:
            self.active_index = 0
        return self.accounts

    def set_active_account(self, index: int) -> bool:
        """切换当前账号"""
        if 0 <= index < len(self.accounts):
            self.active_index = index
            # 数据库密钥严格按账号隔离。切换后必须由 auto-detect 为新账号
            # 重新加载并验证，不能沿用上一账号留在内存中的密钥。
            self.key = None
            self.decrypted_db = None
            self.display_name = None
            self.alias = None
            return True
        return False

    def detect_wxid(self) -> Optional[str]:
        """获取当前 wxid (兼容旧接口)"""
        if not self.accounts:
            self.detect_and_set_accounts()
        return self.wxid

    def detect_databases(self) -> bool:
        """检测数据库文件位置"""
        if not self.wxid or not self.wx_root:
            return False
        acc = self.accounts[self.active_index]
        if not acc.msg_dir:
            return False
        return acc.msg_dir.exists()

    def get_all_msg_dbs(self) -> list[Path]:
        """获取所有消息数据库文件"""
        if not self.msg_dir or not self.msg_dir.exists():
            return []

        dbs = []
        for f in sorted(self.msg_dir.glob("message_*.db")):
            if f.is_file() and f.stat().st_size > 0 and "-" not in f.name:
                dbs.append(f)
        return dbs

    def detect_self_name(self, contact_conn=None):
        """从联系人数据库查找自己的微信名和微信号，并缓存"""
        if not self.wxid or not self.key or not self.wx_root:
            return
        try:
            from .decrypt import DatabaseDecryptor
            from .key_extractor import save_account_info
            if contact_conn is None:
                micromsg = self.micromsg_db
                if not micromsg or not micromsg.exists():
                    return
                dec = DatabaseDecryptor(micromsg, self.key)
                conn = dec.open_decrypted()
            else:
                conn = contact_conn
            cur = conn.cursor()
            base_wxid = self.wxid.rsplit("_", 1)[0] if "_" in self.wxid else self.wxid
            for uname in [self.wxid, base_wxid]:
                cur.execute(
                    "SELECT nick_name, alias FROM contact WHERE username=? LIMIT 1",
                    (uname,),
                )
                row = cur.fetchone()
                if row:
                    self.display_name = row[0] or None
                    self.alias = row[1] or None
                    self.accounts[self.active_index].display_name = self.display_name
                    self.accounts[self.active_index].alias = self.alias
                    # 缓存到 keys 文件
                    save_account_info(self.wxid, self.display_name, self.alias)
                    return
        except Exception:
            pass

    def _load_cached_names(self):
        """从缓存加载账号的显示名称 (无需密钥)"""
        from .key_extractor import load_account_info
        for acc in self.accounts:
            info = load_account_info(acc.wxid)
            if info and isinstance(info, dict):
                acc.display_name = info.get("display_name")
                acc.alias = info.get("alias")

    @property
    def is_ready(self) -> bool:
        """是否准备好进行解密"""
        return bool(self.wxid and self.msg_dir and self.key)

    def to_dict(self) -> dict:
        return {
            "accounts": [a.to_dict() for a in self.accounts],
            "active_index": self.active_index,
            "account_count": len(self.accounts),
            "wx_root": str(self.wx_root) if self.wx_root else None,
            "wxid": self.wxid,
            "display_name": self.display_name,
            "alias": self.alias,
            "msg_dir": str(self.msg_dir) if self.msg_dir else None,
            "has_key": bool(self.key),
            "is_ready": self.is_ready,
            "msg_dbs": [str(f.name) for f in self.get_all_msg_dbs()],
            "wechat_version": (
                "4.x"
                if self.wx_root and self.wx_root.name.lower() == "xwechat_files"
                else None
            ),
            "supports_chat_images": bool(
                self.wx_root and self.wx_root.name.lower() == "xwechat_files"
            ),
            "supports_moments": self.sns_db is not None,
        }


# 全局配置实例
config = WeChatConfig()
