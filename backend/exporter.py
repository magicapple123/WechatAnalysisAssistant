"""
微信解析助手 - 聊天记录导出模块

支持导出格式:
- HTML: 美观的网页聊天记录 (含样式，可直接浏览)
- JSON: 结构化数据
- CSV: 表格格式
- TXT:  纯文本格式
"""
import json
import csv
import base64
import re
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Optional

from .parser_v4 import MessageParserV4
from .time_utils import format_timestamp
from .image_store import ImageDescriptionStore
from .image_service import ImageResolutionError, WeChatImageService
from .voice_store import VoiceTranscriptionStore


class ChatExporter:
    """聊天记录导出器"""

    def __init__(
        self,
        parser: MessageParserV4,
        output_dir: Path,
        *,
        description_store: Optional[ImageDescriptionStore] = None,
        account_id: str = "",
        image_service: Optional[WeChatImageService] = None,
        voice_store: Optional[VoiceTranscriptionStore] = None,
    ):
        self.parser = parser
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.description_store = description_store
        self.account_id = account_id
        self.image_service = image_service
        self.voice_store = voice_store

    def export_chat(
        self,
        talker: str,
        display_name: str,
        fmt: str = "html",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        message_ids: Optional[list[int]] = None,
        message_refs: Optional[list[dict]] = None,
        replace_images_with_descriptions: bool = False,
        replace_voices_with_transcriptions: bool = False,
        embed_images: bool = True,
        html_image_quality: str = "best",
    ) -> Path:
        """
        导出单个聊天的消息

        Args:
            talker: 聊天对象 ID
            display_name: 显示名称
            fmt: 导出格式 (html, json, csv, txt)
            start_time: 起始时间 (Unix 时间戳)
            end_time: 结束时间 (Unix 时间戳)
            message_ids: 指定消息 ID 列表

        Returns:
            导出文件路径
        """
        fmt = str(fmt or "").strip().lower()
        if fmt not in {"html", "json", "csv", "txt"}:
            raise ValueError(f"不支持的导出格式: {fmt}")

        # 获取消息 (带筛选条件)
        all_messages = []
        # 精确消息引用用于处理微信分库后 local_id 被复用的情况。
        if message_refs:
            normalized_refs = []
            for reference in message_refs:
                try:
                    normalized_refs.append(
                        (
                            int(reference.get("id") or 0),
                            int(reference.get("create_time") or 0),
                            str(reference.get("message_key") or ""),
                        )
                    )
                except (AttributeError, TypeError, ValueError):
                    continue
            if not normalized_refs:
                all_messages = []
            else:
                selected_ids = list(dict.fromkeys(item[0] for item in normalized_refs))
                selected_times = [item[1] for item in normalized_refs if item[1]]
                query_start = start_time
                query_end = end_time
                if selected_times:
                    query_start = min(selected_times) if query_start is None else query_start
                    query_end = max(selected_times) if query_end is None else query_end
                all_messages = self._get_all_messages(
                    talker,
                    start_time=query_start,
                    end_time=query_end,
                    message_ids=selected_ids,
                )
                allowed = {}
                for message_id, create_time, message_key in normalized_refs:
                    allowed.setdefault((message_id, create_time), set()).add(message_key)
                all_messages = [
                    message
                    for message in all_messages
                    if self._matches_reference(talker, message, allowed)
                ]
        elif message_ids and len(message_ids) > 0:
            all_messages = self._get_all_messages(
                talker,
                start_time=start_time,
                end_time=end_time,
                message_ids=message_ids,
            )
        else:
            all_messages = self._get_all_messages(
                talker,
                start_time=start_time,
                end_time=end_time,
            )

        all_messages = self._prepare_image_descriptions(
            all_messages,
            talker,
            replace=replace_images_with_descriptions,
        )
        all_messages = self._prepare_voice_transcriptions(all_messages, talker)

        # 按格式导出
        safe_name = _safe_filename(display_name)

        if fmt == "html":
            return self._export_html(
                all_messages,
                display_name,
                talker,
                safe_name,
                embed_images=embed_images and not replace_images_with_descriptions,
                image_quality=html_image_quality,
                replace_voices=replace_voices_with_transcriptions,
            )
        elif fmt == "json":
            return self._export_json(all_messages, display_name, safe_name)
        elif fmt == "csv":
            return self._export_csv(
                all_messages,
                display_name,
                safe_name,
                replace_voices=replace_voices_with_transcriptions,
            )
        elif fmt == "txt":
            return self._export_txt(
                all_messages,
                display_name,
                safe_name,
                replace_voices=replace_voices_with_transcriptions,
            )
        raise AssertionError("validated export format was not dispatched")

    def _get_all_messages(
        self,
        talker: str,
        *,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        message_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        messages = []
        page = 1
        page_size = 500
        while True:
            result = self.parser.get_messages(
                talker,
                page=page,
                page_size=page_size,
                start_time=start_time,
                end_time=end_time,
                message_ids=message_ids,
            )
            messages.extend(result.get("messages", []))
            if page >= int(result.get("total_pages") or 1):
                break
            page += 1
        return messages

    @staticmethod
    def _message_key(talker: str, message: dict) -> str:
        server_id = str(message.get("server_id") or "")
        local_id = int(message.get("id") or 0)
        create_time = int(message.get("create_time") or 0)
        identity = server_id if server_id and server_id != "0" else str(local_id)
        return f"{talker}:{identity}:{create_time}:{local_id}"

    @classmethod
    def _matches_reference(
        cls,
        talker: str,
        message: dict,
        allowed: dict[tuple[int, int], set[str]],
    ) -> bool:
        key = (
            int(message.get("id") or 0),
            int(message.get("create_time") or 0),
        )
        expected_keys = allowed.get(key)
        if not expected_keys:
            return False
        return "" in expected_keys or cls._message_key(talker, message) in expected_keys

    def export_all_chats(
        self,
        fmt: str = "html",
        replace_images_with_descriptions: bool = False,
        replace_voices_with_transcriptions: bool = False,
        embed_images: bool = True,
        html_image_quality: str = "best",
    ) -> Path:
        """
        导出所有聊天记录

        Returns:
            包含所有导出文件的目录路径，或压缩包路径
        """
        fmt = str(fmt or "").strip().lower()
        if fmt not in {"html", "json", "csv", "txt"}:
            raise ValueError(f"不支持的导出格式: {fmt}")

        contacts = self.parser.get_contacts()
        export_subdir = self.output_dir / f"all_chats_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        export_subdir.mkdir(parents=True, exist_ok=True)

        exported_files = []
        for contact in contacts:
            try:
                filepath = self.export_chat(
                    contact["talker"],
                    contact["display_name"],
                    fmt=fmt,
                    replace_images_with_descriptions=replace_images_with_descriptions,
                    replace_voices_with_transcriptions=replace_voices_with_transcriptions,
                    embed_images=embed_images,
                    html_image_quality=html_image_quality,
                )
                exported_files.append(filepath)
            except Exception as e:
                print(f"[ERROR] 导出失败 {contact['display_name']}: {e}")

        # 创建索引文件
        self._create_index_html(contacts, export_subdir)

        # 打包为 ZIP
        zip_path = export_subdir.with_suffix(".zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in exported_files:
                zf.write(file, file.name)
            # 也打包索引文件
            index_file = export_subdir / "index.html"
            if index_file.exists():
                zf.write(index_file, "index.html")

        return zip_path

    def _prepare_image_descriptions(
        self,
        messages: list[dict],
        talker: str,
        *,
        replace: bool,
    ) -> list[dict]:
        """Attach cached descriptions and optionally compute export content.

        Parser output is copied so a text replacement never mutates the
        in-memory chat view or triggers model recognition during export.
        """
        prepared = [dict(message) for message in messages]
        if not self.description_store or not self.account_id:
            return prepared
        records = self.description_store.get_many(self.account_id, talker, prepared)
        for message in prepared:
            try:
                is_image = (int(message.get("type") or 0) & 0xFFFFFFFF) in (2, 3)
            except (TypeError, ValueError):
                is_image = False
            if not is_image:
                continue
            key = (
                int(message.get("id") or 0),
                int(message.get("create_time") or 0),
                str(message.get("server_id") or ""),
            )
            record = records.get(key)
            description = ""
            if record and record.get("status") == "success":
                description = str(record.get("description") or "").strip()
            message["image_description"] = description
            if replace and description:
                message["content"] = f"[图片内容] {description}"
                message["content_preview"] = message["content"][:100]
        return prepared

    def _prepare_voice_transcriptions(
        self,
        messages: list[dict],
        talker: str,
    ) -> list[dict]:
        """Attach cached voice transcripts without invoking an ASR provider.

        Store lookups are scoped by both the selected account and the complete
        message identity.  This prevents a reused WeChat ``local_id`` from
        attaching another message's transcription during export.
        """

        prepared = [dict(message) for message in messages]
        voice_messages = [
            message for message in prepared if self._is_voice_message(message)
        ]
        records = {}
        if self.voice_store and self.account_id and voice_messages:
            records = self.voice_store.get_many(
                self.account_id,
                talker,
                voice_messages,
            )

        for message in prepared:
            message["message_key"] = self._message_key(talker, message)
            if not self._is_voice_message(message):
                continue
            key = (
                int(message.get("id") or 0),
                int(message.get("create_time") or 0),
                str(message.get("server_id") or ""),
            )
            record = records.get(key)
            success = bool(record and record.get("status") == "success")
            message["voice_transcription"] = (
                str(record.get("transcription") or "").strip() if success else ""
            )
            message["voice_transcription_status"] = (
                str(record.get("status") or "") if record else ""
            )
        return prepared

    @staticmethod
    def _is_voice_message(message: dict) -> bool:
        try:
            return (int(message.get("type") or 0) & 0xFFFFFFFF) in (4, 34)
        except (AttributeError, TypeError, ValueError):
            return False

    @classmethod
    def _voice_export_content(cls, message: dict, *, replace: bool) -> str:
        content = str(message.get("content") or "")
        transcription = str(message.get("voice_transcription") or "").strip()
        if replace and transcription and cls._is_voice_message(message):
            return f"[语音转文字] {transcription}"
        return content

    def _export_html(
        self,
        messages: list[dict],
        display_name: str,
        talker: str,
        safe_name: str,
        *,
        embed_images: bool = True,
        image_quality: str = "best",
        replace_voices: bool = False,
    ) -> Path:
        """导出为 HTML 格式"""
        if image_quality not in ("thumbnail", "best"):
            raise ValueError("HTML 图片清晰度必须是 thumbnail 或 best")
        filepath = self.output_dir / f"{safe_name}.html"

        messages_html = []
        current_date = ""

        for msg in messages:
            # 日期分隔符
            date_str = msg.get("date_str", "")
            if date_str != current_date:
                current_date = date_str
                messages_html.append(
                    f'<div class="date-divider"><span>{current_date}</span></div>'
                )

            # 消息气泡
            css_class = "message-self" if msg["is_sender"] else "message-other"
            time_str = format_timestamp(msg["create_time"])[-8:] if msg.get("create_time") else ""

            display_content = self._voice_export_content(
                msg,
                replace=replace_voices,
            )
            content = _escape_html(display_content).replace("\n", "<br>")
            try:
                is_image = (int(msg.get("type") or 0) & 0xFFFFFFFF) in (2, 3)
            except (TypeError, ValueError):
                is_image = False
            if is_image and embed_images:
                content = self._embedded_image_html(
                    talker,
                    msg,
                    image_quality=image_quality,
                    fallback=content,
                )

            # 语音消息：未替换时仍展示已有转写文字
            transcription_html = ""
            if (
                not replace_voices
                and self._is_voice_message(msg)
                and str(msg.get("voice_transcription") or "").strip()
            ):
                transcription_html = (
                    '<div class="voice-transcription">'
                    f"语音转写：{_escape_html(str(msg['voice_transcription']).strip())}"
                    "</div>"
                )

            # 群聊发送者名称
            sender_html = ""
            sender_name = msg.get("sender_name", "")
            if sender_name and not msg["is_sender"]:
                sender_html = f'<div class="sender-name">{_escape_html(sender_name)}</div>'

            messages_html.append(f"""
            <div class="{css_class}">
                <div class="message-bubble">
                    {sender_html}
                    <div class="message-content">{content}</div>
                    {transcription_html}
                    <div class="message-time">{time_str}</div>
                </div>
            </div>
            """)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>与 {_escape_html(display_name)} 的聊天记录</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #f0f0f0;
            padding: 20px;
        }}
        .chat-header {{
            background: #07c160;
            color: white;
            padding: 15px 20px;
            border-radius: 10px 10px 0 0;
            font-size: 18px;
            font-weight: bold;
            text-align: center;
        }}
        .chat-container {{
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .chat-messages {{
            padding: 20px;
            min-height: 60vh;
        }}
        .date-divider {{
            text-align: center;
            margin: 15px 0;
        }}
        .date-divider span {{
            background: #d9d9d9;
            color: #666;
            font-size: 12px;
            padding: 3px 10px;
            border-radius: 3px;
        }}
        .message-self, .message-other {{
            margin-bottom: 15px;
            display: flex;
        }}
        .message-self {{
            justify-content: flex-end;
        }}
        .message-other {{
            justify-content: flex-start;
        }}
        .message-bubble {{
            max-width: 70%;
            padding: 10px 15px;
            border-radius: 10px;
            position: relative;
        }}
        .message-self .message-bubble {{
            background: #95ec69;
            border-top-right-radius: 2px;
        }}
        .message-other .message-bubble {{
            background: white;
            border: 1px solid #e0e0e0;
            border-top-left-radius: 2px;
        }}
        .sender-name {{
            font-size: 12px;
            color: #07c160;
            margin-bottom: 2px;
            font-weight: 500;
        }}
        .message-content {{
            font-size: 15px;
            line-height: 1.5;
            word-break: break-word;
        }}
        .chat-image {{
            margin: 0;
        }}
        .chat-image img {{
            display: block;
            max-width: min(100%, 680px);
            max-height: 80vh;
            width: auto;
            height: auto;
            border-radius: 6px;
            object-fit: contain;
        }}
        .image-description {{
            margin-top: 6px;
            color: #666;
            font-size: 12px;
            line-height: 1.45;
        }}
        .voice-transcription {{
            margin-top: 6px;
            padding: 6px 10px;
            background: rgba(0,0,0,0.04);
            border-left: 3px solid #f97316;
            border-radius: 4px;
            font-size: 13px;
            line-height: 1.5;
            color: #666;
        }}
        .message-time {{
            font-size: 11px;
            color: #999;
            text-align: right;
            margin-top: 4px;
        }}
        .chat-footer {{
            text-align: center;
            padding: 20px;
            color: #999;
            font-size: 12px;
            border-top: 1px solid #eee;
        }}
        @media (max-width: 600px) {{
            body {{ padding: 0; }}
            .chat-container {{ border-radius: 0; }}
            .chat-header {{ border-radius: 0; }}
            .message-bubble {{ max-width: 85%; }}
        }}
    </style>
</head>
<body>
    <div class="chat-container">
        <div class="chat-header">与 {_escape_html(display_name)} 的聊天记录</div>
        <div class="chat-messages">
            {"".join(messages_html)}
        </div>
        <div class="chat-footer">
            共 {len(messages)} 条消息 | 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </div>
    </div>
</body>
</html>"""

        filepath.write_text(html, encoding="utf-8")
        return filepath

    def _embedded_image_html(
        self,
        talker: str,
        message: dict,
        *,
        image_quality: str,
        fallback: str,
    ) -> str:
        """Return a self-contained data URI, or safe text when unavailable."""
        if not self.image_service:
            return fallback
        try:
            image, image_bytes = self.image_service.get_image_bytes(
                talker=talker,
                message_id=int(message.get("id") or 0),
                create_time=int(message.get("create_time") or 0),
                server_id=message.get("server_id"),
                purpose="display",
                quality=image_quality,
            )
        except (ImageResolutionError, OSError, TypeError, ValueError):
            return fallback
        encoded = base64.b64encode(image_bytes).decode("ascii")
        description = str(message.get("image_description") or "").strip()
        alt = _escape_html(description or "聊天图片")
        description_html = (
            f'<div class="image-description">{_escape_html(description)}</div>'
            if description
            else ""
        )
        return (
            '<figure class="chat-image">'
            f'<img src="data:{image.mime_type};base64,{encoded}" alt="{alt}" '
            'loading="lazy">'
            f"{description_html}</figure>"
        )

    def _export_json(self, messages: list[dict], display_name: str, safe_name: str) -> Path:
        """导出为 JSON 格式"""
        filepath = self.output_dir / f"{safe_name}.json"

        export_data = {
            "chat_name": display_name,
            "export_time": datetime.now().isoformat(),
            "message_count": len(messages),
            "messages": [
                {
                    "id": m["id"],
                    "type": m["type_name"],
                    "is_self": m["is_sender"],
                    "sender_name": m.get("sender_name", ""),
                    "content": m["content"],
                    "image_description": m.get("image_description", ""),
                    "voice_transcription": m.get("voice_transcription", ""),
                    "voice_transcription_status": m.get(
                        "voice_transcription_status", ""
                    ),
                    "message_key": m.get("message_key", ""),
                    "server_id": m.get("server_id", ""),
                    "time": format_timestamp(m["create_time"]),
                    "timestamp": m["create_time"],
                }
                for m in messages
            ],
        }

        filepath.write_text(
            json.dumps(export_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return filepath

    def _export_csv(
        self,
        messages: list[dict],
        display_name: str,
        safe_name: str,
        *,
        replace_voices: bool = False,
    ) -> Path:
        """导出为 CSV 格式"""
        filepath = self.output_dir / f"{safe_name}.csv"

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["序号", "时间", "发送者", "类型", "内容"])

            for i, msg in enumerate(messages, 1):
                if msg["is_sender"]:
                    sender = "我"
                else:
                    sender = msg.get("sender_name") or display_name
                writer.writerow([
                    i,
                    format_timestamp(msg["create_time"]),
                    sender,
                    msg["type_name"],
                    _escape_csv_formula(
                        self._voice_export_content(msg, replace=replace_voices)
                    ),
                ])

        return filepath

    def _export_txt(
        self,
        messages: list[dict],
        display_name: str,
        safe_name: str,
        *,
        replace_voices: bool = False,
    ) -> Path:
        """导出为 TXT 格式"""
        filepath = self.output_dir / f"{safe_name}.txt"

        # 计算时间范围
        timestamps = [m["create_time"] for m in messages if m.get("create_time")]
        time_range = ""
        if timestamps:
            from_ts = min(timestamps)
            to_ts = max(timestamps)
            time_range = (
                f"时间范围: {datetime.fromtimestamp(from_ts).strftime('%Y-%m-%d %H:%M:%S')}"
                f" 至 {datetime.fromtimestamp(to_ts).strftime('%Y-%m-%d %H:%M:%S')}"
            )

        lines = [
            f"与 {display_name} 的聊天记录",
            f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if time_range:
            lines.append(time_range)
        lines += [
            f"消息数量: {len(messages)}",
            "=" * 50,
            "",
        ]

        current_date = ""
        for msg in messages:
            ts = msg.get("create_time", 0)
            if ts:
                date_str = datetime.fromtimestamp(ts).strftime("%Y年%m月%d日")
            else:
                date_str = ""
            if date_str != current_date:
                current_date = date_str
                lines.append(f"\n--- {current_date} ---\n")

            time_str = format_timestamp(msg["create_time"])
            if msg["is_sender"]:
                sender = "我"
            else:
                sender = msg.get("sender_name") or display_name
            lines.append(f"[{time_str}] {sender}:")
            lines.append(self._voice_export_content(msg, replace=replace_voices))
            lines.append("")

        filepath.write_text("\n".join(lines), encoding="utf-8")
        return filepath

    def _create_index_html(self, contacts: list[dict], export_dir: Path):
        """创建导出索引页面"""
        links_html = []
        for c in contacts:
            safe_name = _safe_filename(c["display_name"])
            links_html.append(
                f'<li><a href="{safe_name}.html">{_escape_html(c["display_name"])}</a> '
                f'<span class="meta">({c["message_count"]} 条消息)</span></li>'
            )

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>微信解析助手 - 导出索引</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
               max-width: 600px; margin: 40px auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; }}
        ul {{ list-style: none; padding: 0; }}
        li {{ background: white; margin: 8px 0; padding: 12px 16px; border-radius: 8px;
              box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        a {{ color: #07c160; text-decoration: none; font-size: 16px; }}
        .meta {{ color: #999; font-size: 13px; }}
    </style>
</head>
<body>
    <h1>📱 微信解析助手</h1>
    <p>导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p>共 {len(contacts)} 个聊天</p>
    <ul>
        {"".join(links_html)}
    </ul>
</body>
</html>"""
        (export_dir / "index.html").write_text(html, encoding="utf-8")


def _safe_filename(name: str) -> str:
    """生成安全的文件名"""
    # 移除不安全字符
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    # 限制长度
    if len(name) > 50:
        name = name[:50]
    return name.strip() or "chat"


def _escape_html(text: str) -> str:
    """HTML 转义"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _escape_csv_formula(value: object) -> str:
    """Prevent spreadsheet applications from executing exported content."""
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text
