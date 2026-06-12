#!/usr/bin/env python3
"""
Fake Zotero Backend — 模拟 Zotero 桌面客户端 HTTP 服务器
接收 Zotero Connector 浏览器扩展提取的论文元数据和附件

协议: http://127.0.0.1:23119/connector/{method}
参考: https://www.zotero.org/support/dev/client_coding/connector_http_server
"""

import json
import os
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import sys

HOST = "127.0.0.1"
PORT = 23119
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
VERSION = "6.0.0"
DEBUG = False  # 启动时加 --debug 开启

# ── 偏好配置 ─────────────────────────────────────────────────────
# 关键开关: supportsAttachmentUpload=true → connector 在浏览器端下载 PDF
# 然后通过 POST /connector/saveAttachment 把二进制发给我们
PREFERENCES = {
    "downloadAssociatedFiles": True,
    "reportActiveURL": False,
    "automaticSnapshots": False,
    "googleDocsAddAnnotationEnabled": False,
    "googleDocsCitationExplorerEnabled": False,
    "supportsAttachmentUpload": True,       # 浏览器下载 PDF 后上传给我们
    "supportsTagsAutocomplete": False,
    "canUserAddNote": False,
    # 不设 translatorsHash — connector 就不会试图从我们这更新 translators，
    # 它会用自己的内置 translators，页面检测才能正常工作
}


def _session_dir(session_id):
    """output/{sessionID}/"""
    d = os.path.join(OUTPUT_DIR, session_id)
    os.makedirs(d, exist_ok=True)
    return d


def _append_index(entry):
    """追加一条记录到 _index.jsonl"""
    path = os.path.join(OUTPUT_DIR, "_index.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class FakeZoteroHandler(BaseHTTPRequestHandler):

    # ── 响应辅助 ─────────────────────────────────────────────────
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self._status_code = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Zotero-Version", VERSION)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status=200):
        self._status_code = status
        self.send_response(status)
        self.send_header("X-Zotero-Version", VERSION)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json_body(self):
        raw = self._read_body()
        if not raw:
            return {}
        return json.loads(raw)

    # ── 路由 ─────────────────────────────────────────────────────
    def do_GET(self):
        self._route(urlparse(self.path).path, is_get=True)

    def do_POST(self):
        self._route(urlparse(self.path).path, is_get=False)

    def do_OPTIONS(self):
        self._status_code = 200
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Zotero-Version, X-Zotero-Connector-API-Version, X-Metadata",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── DEBUG: 打印原始请求 ─────────────────────────────────
    def _debug_dump_request(self):
        """打印完整请求头和 body（仅在 DEBUG=True 时生效）"""
        if not DEBUG:
            return
        print(f"\n  -- {self.command} {self.path}")
        for k, v in self.headers.items():
            print(f"     {k}: {v}")
        length = int(self.headers.get("Content-Length", 0))
        raw: bytes | None = None
        if length > 0:
            raw = self._read_body()
            ct = self.headers.get("Content-Type", "")
            if raw is None:
                print(f"     [BODY: {length} bytes, 读取失败]")
            elif not ct.startswith("application/json"):
                print(f"     [BODY: {length} bytes, Content-Type: {ct}]")
                if length < 200:
                    print(f"     {raw!r}")
            else:
                preview = raw[:2000].decode("utf-8", errors="replace")
                if len(raw) > 2000:
                    preview += f"\n     … ({length - 2000} more bytes truncated)"
                for line in preview.splitlines():
                    print(f"     {line}")
        else:
            print("     (no body)")
        self._cached_body = raw if length > 0 else None

    def _read_body(self):
        if hasattr(self, '_cached_body'):
            raw = self._cached_body
            del self._cached_body
            return raw
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        return self.rfile.read(length)

    def _route(self, path, is_get=False):
        self._debug_dump_request()
        prefix = "/connector/"
        if not path.startswith(prefix):
            return self._send_empty(404)

        method = path[len(prefix):]
        router = {
            "ping":                   self._handle_ping,
            "saveItems":              self._handle_save_items,
            "sessionProgress":        self._handle_session_progress,
            "saveSnapshot":           self._handle_save_snapshot,
            "saveSingleFile":         self._handle_save_single_file,
            "saveAttachment":         self._handle_save_attachment,
            "getTranslatorCode":      self._handle_get_translator_code,
            "getTranslators":         self._handle_get_translators,
            "getSelectedCollection":  self._handle_get_selected_collection,
            "hasAttachmentResolvers":    self._handle_has_attachment_resolvers,
            "saveAttachmentFromResolver": self._handle_save_attachment_from_resolver,
            "selectItems":               self._handle_select_items,
            "delaySync":                 self._handle_delay_sync,
        }
        handler = router.get(method)
        if handler:
            handler()
        else:
            print(f"  ⚠ 未实现的端点: {method}")
            self._send_json({"status": "ok"})

    # ── 端点实现 ─────────────────────────────────────────────────
    def _handle_ping(self):
        body = self._read_json_body()
        print(f"  📡 ping (activeURL: {body.get('activeURL', '—')})")
        self._send_json({"prefs": PREFERENCES, "version": VERSION})

    def _handle_save_items(self):
        body = self._read_json_body()
        session_id = body.get("sessionID", "unknown")
        uri = body.get("uri", "")
        items = body.get("items", [])
        ts = datetime.now(timezone.utc)

        sdir = _session_dir(session_id)
        save_path = os.path.join(sdir, "save.json")

        # 带元信息的完整记录
        record = {
            "_meta": {
                "received_at": ts.isoformat(),
                "sessionID": session_id,
                "uri": uri,
                "item_count": len(items),
            },
            "items": items,
        }
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        # 索引（每篇一条）
        received_at = ts.isoformat()
        for item in items:
            creators = item.get("creators", [])
            first_author = (
                f"{creators[0].get('lastName', '')}, {creators[0].get('firstName', '')}"
                if creators else ""
            )
            _append_index({
                "kind": "item",
                "file": f"{session_id}/save.json",
                "received_at": received_at,
                "sessionID": session_id,
                "title": item.get("title", ""),
                "itemType": item.get("itemType", ""),
                "firstAuthor": first_author,
                "authorCount": len(creators),
                "DOI": item.get("DOI", ""),
                "publicationTitle": item.get("publicationTitle", ""),
                "url": item.get("url", ""),
                "uri": uri,
            })

        # 控制台
        print(f"  📄 saveItems — {len(items)} 篇 → {session_id}/")
        for i, item in enumerate(items):
            t = item.get("title", "(无标题)")
            c = item.get("creators", [])
            a = ", ".join(
                f"{x.get('firstName', '')} {x.get('lastName', '')}".strip()
                for x in c[:3]
            )
            if len(c) > 3:
                a += " et al."
            print(f"     [{i+1}] [{item.get('itemType','?')}] {t}")
            if a:
                print(f"          作者: {a}")
            if item.get("DOI"):
                print(f"          DOI:  {item['DOI']}")
            if item.get("publicationTitle"):
                print(f"          期刊: {item['publicationTitle']}")

        # PDF 下载由 connector 的 saveAttachment 端点处理（浏览器环境）
        # 或由 Zotero 桌面端的 OA 解析器处理

        # connector expects items back
        for item in items:
            for att in item.get("attachments", []):
                att["progress"] = 100
        self._send_json({"items": items})

    def _handle_session_progress(self):
        body = self._read_json_body()
        sid = body.get("sessionID", "unknown")
        print(f"  ⏳ sessionProgress ({sid}) → done")
        self._send_json({"done": True, "items": []})

    def _handle_save_snapshot(self):
        body = self._read_json_body()
        sid = body.get("sessionID", "unknown")
        t = body.get("title", "")
        print(f"  📸 saveSnapshot — {t} ({sid})")
        self._send_json({"status": "ok"})

    def _handle_save_single_file(self):
        body = self._read_json_body()
        session_id = body.get("sessionID", "unknown")
        title = body.get("title", "")

        sdir = _session_dir(session_id)
        content = body.get("snapshotContent")
        if content:
            path = os.path.join(sdir, "snapshot.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  💾 saveSingleFile — {title} → {session_id}/snapshot.html")
        else:
            print(f"  💾 saveSingleFile — {title} (无快照内容)")
        self._send_json({"status": "ok"})

    def _handle_save_attachment(self):
        """
        接收二进制附件（PDF / EPUB 等）
        connector 在浏览器后台页下载好后 POST 过来
        """
        session_id = (
            parse_qs(urlparse(self.path).query).get("sessionID", [None])[0]
            or "unknown"
        )
        raw = self._read_body()
        meta_str = self.headers.get("X-Metadata", "{}")
        content_type = self.headers.get("Content-Type", "application/octet-stream")

        try:
            meta = json.loads(meta_str)
        except json.JSONDecodeError:
            meta = {}

        att_id = meta.get("id", "unknown")
        title = meta.get("title", "attachment")
        source_url = meta.get("url", "")

        # 扩展名
        ext_map = {
            "application/pdf": ".pdf",
            "text/html": ".html",
            "application/epub+zip": ".epub",
            "image/png": ".png",
            "image/jpeg": ".jpg",
        }
        ext = ext_map.get(content_type, ".bin")

        sdir = _session_dir(session_id)
        filename = f"{att_id}{ext}"
        filepath = os.path.join(sdir, filename)

        if raw:
            with open(filepath, "wb") as f:
                f.write(raw)
            print(f"  📎 saveAttachment — {title} ({content_type}, {len(raw)} bytes)"
                  f" → {session_id}/{filename}")

            # 索引
            _append_index({
                "kind": "attachment",
                "file": f"{session_id}/{filename}",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "sessionID": session_id,
                "title": title,
                "contentType": content_type,
                "size": len(raw),
                "sourceURL": source_url,
                "attachmentID": att_id,
            })
        else:
            print(f"  📎 saveAttachment — {title} (空)")

        self._send_json({"status": "ok"})

    def _handle_get_translators(self):
        print("  🔌 getTranslators → 空列表")
        self._send_json([])

    def _handle_get_selected_collection(self):
        """返回当前选中的 library/collection 信息及可选目标列表"""
        print("  📁 getSelectedCollection → 默认收藏")
        self._send_json({
            "libraryID": 1,
            "libraryName": "My Library",
            "libraryEditable": True,
            "filesEditable": True,
            "editable": True,
            "id": 1,
            "name": "My Library",
            "targets": [
                {
                    "id": "L1",
                    "name": "My Library",
                    "filesEditable": True,
                    "level": 0,
                }
            ],
            "tags": {},
        })

    def _handle_get_translator_code(self):
        print("  🔌 getTranslatorCode (空)")
        self._send_json({"status": "ok"})

    def _handle_delay_sync(self):
        """保活心跳 — 每 7.5s 一次，fire-and-forget"""
        self._send_empty(200)

    def _handle_has_attachment_resolvers(self):
        self._send_json(False)

    def _handle_save_attachment_from_resolver(self):
        self._read_json_body()
        self._send_json("")

    def _handle_select_items(self):
        print("  🎯 selectItems → 空")
        self._send_json({"items": [], "sessionID": str(uuid.uuid4())})

def main():
    global DEBUG
    if "--debug" in sys.argv:
        DEBUG = True

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    server = HTTPServer((HOST, PORT), FakeZoteroHandler)
    print("Fake Zotero Backend")
    print(f"  监听  http://{HOST}:{PORT}")
    print(f"  输出  {OUTPUT_DIR}")
    print(f"  调试  {'开启' if DEBUG else '关闭'}  (加 --debug 开启)")
    print()
    print("  浏览器下载 PDF → 自动存到 session 文件夹")
    print("  _index.jsonl → 可检索")
    print()
    print("  按 Ctrl+C 停止")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹  停止服务")
        server.server_close()


if __name__ == "__main__":
    main()
