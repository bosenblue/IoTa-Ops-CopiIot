"""IoTa Ops local API: persistent demo data, runbook search, and diagnosis."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("IOTA_DB_PATH", str(ROOT / "data" / "iota_ops.db")))
UPLOAD_DIR = Path(os.getenv("IOTA_UPLOAD_DIR", str(ROOT / "data" / "runbooks")))
PORT = int(os.getenv("IOTA_PORT", "8000"))
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
MAX_UPLOAD_BYTES = 1_000_000

SEED_DEVICES = [
    ("TH-0081", "仓储一区 · 温湿度传感器", "异常", "14:42", "GW-204", "82%", "31.8 °C"),
    ("SN-2048", "仓储一区 · 门磁传感器", "离线", "14:35", "GW-204", "76%", "无心跳"),
    ("BT-0316", "冷链二区 · 温度传感器", "异常", "14:18", "GW-112", "8%", "5.2 °C"),
    ("SN-2051", "仓储一区 · 烟雾传感器", "离线", "14:09", "GW-204", "64%", "无心跳"),
    ("TH-0093", "冷链一区 · 温湿度传感器", "在线", "14:46", "GW-112", "91%", "4.1 °C"),
]
SEED_ALERTS = [
    ("AL-3108", "传感器温度过高", "TH-0081 · 仓储一区", "严重", "14:42", "待处理"),
    ("AL-3107", "设备心跳丢失", "SN-2048 · 网关 GW-204", "警告", "14:35", "待处理"),
    ("AL-3106", "电池电量低于阈值", "BT-0316 · 冷链二区", "警告", "14:18", "处理中"),
    ("AL-3105", "上行数据超时", "GW-204 · 仓储一区", "严重", "14:12", "待处理"),
]
SEED_DOCS = [
    ("网关离线排查手册", "网关离线时，先检查网关网络连接、上行链路和供电状态。确认网关恢复后，观察关联设备心跳是否恢复。若仍有设备离线，再逐台检查终端电量、天线和信号质量。多个设备在相近时间失联且共用同一网关时，优先排查网关和共享网络链路。", "运维标准 · v2.1"),
    ("温湿度传感器校准与异常处理", "温度读数越过现场阈值时，先核对设备编号、安装区域和最近一次校准时间。检查探头是否受热源直吹、冷凝或安装位置变化影响。与校准过的参考设备比对后再判断传感器故障。未确认前保留原始读数并记录时间。", "设备手册 · v1.4"),
    ("电池低电量巡检规范", "设备电量低于 15% 时生成巡检任务。更换电池前检查电池型号、接点氧化和环境温度。更换后确认设备重新上线并上报电量。电量低与弱信号同时出现时，检查安装位置和重传频次。", "巡检规范 · v1.2"),
]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(conn: sqlite3.Connection, title: str, detail: str, kind: str = "info") -> None:
    conn.execute("INSERT INTO audit(title, detail, kind, created_at) VALUES (?, ?, ?, ?)", (title, detail, kind, now_iso()))


def init_db() -> None:
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, site TEXT NOT NULL, status TEXT NOT NULL,
                last_seen TEXT NOT NULL, gateway TEXT NOT NULL, battery TEXT NOT NULL,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, device TEXT NOT NULL,
                severity TEXT NOT NULL, occurred_at TEXT NOT NULL, state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE,
                meta TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL, content TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                detail TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnoses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL,
                mode TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0:
            conn.executemany("INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?)", SEED_DEVICES)
        if conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0:
            conn.executemany("INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?)", SEED_ALERTS)
        if conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0:
            for title, body, meta in SEED_DOCS:
                doc_id = conn.execute("INSERT INTO documents(title, meta, created_at) VALUES (?, ?, ?)", (title, meta, now_iso())).lastrowid
                add_chunks(conn, doc_id, body)
            log_event(conn, "演示数据初始化完成", "创建示例设备、告警和运维手册", "info")


def add_chunks(conn: sqlite3.Connection, doc_id: int, text: str) -> None:
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks: list[str] = []
    for paragraph in paragraphs:
        while len(paragraph) > 420:
            split_at = paragraph.rfind("。", 0, 420)
            split_at = split_at + 1 if split_at > 100 else 420
            chunks.append(paragraph[:split_at].strip())
            paragraph = paragraph[split_at:].strip()
        if paragraph:
            chunks.append(paragraph)
    conn.executemany("INSERT INTO chunks(document_id, chunk_index, content) VALUES (?, ?, ?)", [(doc_id, i, chunk) for i, chunk in enumerate(chunks)])


def search_runbooks(conn: sqlite3.Connection, query: str, limit: int = 4) -> list[dict[str, str]]:
    terms: list[str] = []
    for token in re.findall(r"[a-zA-Z0-9_-]+|[\u4e00-\u9fff]+", query.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            terms.append(token)
    rows = conn.execute("SELECT d.title, c.content FROM chunks c JOIN documents d ON d.id=c.document_id").fetchall()
    ranked = []
    for row in rows:
        content = row["content"]
        lower = content.lower()
        score = sum(lower.count(term) for term in terms)
        if score:
            ranked.append((score, row["title"], content))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [{"title": title, "content": content} for _, title, content in ranked[:limit]]


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="IoTa Ops API", version="0.2.0", lifespan=lifespan)


class DiagnosisRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class AlertUpdate(BaseModel):
    state: str = Field(pattern="^(待处理|处理中|已恢复)$")


def demo_diagnosis(question: str, context: str, references: list[dict[str, str]]) -> dict[str, object]:
    gateway_case = "网关" in question or "离线" in question or "GW-204" in question
    if gateway_case:
        cause = "多台关联设备在相近时间失去心跳，且共用网关 GW-204。结合网关上行超时记录，更可能是网关网络波动，而非设备同时故障。"
        evidence = "12 台设备在 14:32–14:35 离线；GW-204 同时出现 3 次上行超时；其余网关运行正常。以上为模拟观测数据。"
        steps = ["检查 GW-204 网络连接与供电状态", "确认网关恢复后观察关联设备心跳", "若仍离线，再逐台检查终端电量与信号"]
    elif "电量" in question or "电池" in question or "BT-0316" in question:
        cause = "BT-0316 的模拟电量为 8%，低于巡检阈值，建议优先安排电池检查。"
        evidence = "设备 BT-0316 当前示例电量 8%，关联告警 AL-3106 为电池低电量。"
        steps = ["核对电池型号与接点状态", "更换后确认设备重新上线并上报电量", "若同时存在弱信号，检查设备安装位置"]
    elif "温度" in question or "TH-0081" in question:
        cause = "TH-0081 当前模拟读数为 31.8 °C，并触发温度告警。先核验安装环境和校准情况，再判断设备异常。"
        evidence = "设备 TH-0081 位于仓储一区；示例读数 31.8 °C；存在 AL-3108 温度告警。"
        steps = ["核对现场参考温度和设备安装位置", "检查最近校准时间及探头状态", "确认环境因素后再决定是否维修"]
    else:
        cause = "当前数据不足以对该问题作出可靠判断。请提供设备编号或关联告警编号。"
        evidence = "本次诊断只可访问本地示例设备、告警和已索引运维手册。"
        steps = ["在设备管理中搜索设备编号", "确认设备最近心跳和活跃告警", "补充具体现象后重新诊断"]
    return {
        "mode": "demo", "title": question[:22] + ("…" if len(question) > 22 else ""),
        "cause": cause, "evidence": evidence,
        "steps": steps,
        "reference": "；".join(dict.fromkeys(ref["title"] for ref in references)) if references else "未找到匹配的已索引文档",
        "context": context,
        "sources": references,
    }


def model_diagnosis(question: str, context: str, references: list[dict[str, str]]) -> dict[str, object]:
    reference_text = "\n".join(f"[{item['title']}] {item['content']}" for item in references) or "未检索到相关运维手册。"
    payload = {
        "model": MODEL_NAME, "temperature": 0.2, "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "你是 IoT 运维助手。仅依据提供的设备、告警和手册内容诊断；区分事实与推断；不要声称执行过操作。用中文返回 JSON：cause (string), evidence (string), steps (string array), reference (string)。引用只能来自给定手册标题。"},
            {"role": "user", "content": f"运维上下文：\n{context}\n\n相关手册片段：\n{reference_text}\n\n问题：{question}"},
        ],
    }
    request = Request(f"{MODEL_BASE_URL}/chat/completions", data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {MODEL_API_KEY}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=45) as response:
            result = json.loads(response.read())
        answer = json.loads(result["choices"][0]["message"]["content"])
        steps = answer.get("steps", [])
        if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
            raise ValueError("模型返回的 steps 格式无效")
        return {"mode": "model", "title": question[:22] + ("…" if len(question) > 22 else ""), "cause": str(answer.get("cause", "模型未提供原因")), "evidence": str(answer.get("evidence", "模型未提供证据")), "steps": steps[:6], "reference": str(answer.get("reference", "未找到正式知识库引用")), "sources": references}
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except OSError:
            detail = str(exc)
        raise HTTPException(status_code=502, detail=f"模型请求失败（HTTP {exc.code}）：{detail}") from exc
    except (URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"模型请求失败：{str(exc)[:300]}") from exc


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "model" if MODEL_API_KEY else "demo", "database": str(DB_PATH.name)}


@app.get("/api/devices")
def list_devices(q: str = "", state: str = "") -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
    result = [dict(row) for row in rows]
    if state:
        result = [row for row in result if row["status"] == state]
    if q:
        result = [row for row in result if q.lower() in " ".join(row.values()).lower()]
    return result


@app.get("/api/alerts")
def list_alerts(q: str = "", state: str = "") -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM alerts ORDER BY occurred_at DESC, id").fetchall()
    result = [dict(row) for row in rows]
    if state:
        result = [row for row in result if row["state"] == state]
    if q:
        result = [row for row in result if q.lower() in " ".join(row.values()).lower()]
    return result


@app.patch("/api/alerts/{alert_id}")
def update_alert(alert_id: str, update: AlertUpdate) -> dict[str, str]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not row:
            raise HTTPException(404, "告警不存在")
        conn.execute("UPDATE alerts SET state=? WHERE id=?", (update.state, alert_id))
        log_event(conn, f"告警 {alert_id} 标记为{update.state}", f"操作人：运维员 OP · {row['name']}", "warn")
    return {"id": alert_id, "state": update.state}


@app.get("/api/documents")
def list_documents(q: str = "") -> list[dict[str, object]]:
    with connect() as conn:
        rows = conn.execute("SELECT d.id, d.title, d.meta, d.created_at, COUNT(c.id) AS chunks FROM documents d LEFT JOIN chunks c ON c.document_id=d.id GROUP BY d.id ORDER BY d.id").fetchall()
    result = [dict(row) for row in rows]
    return [row for row in result if not q or q.lower() in f"{row['title']} {row['meta']}".lower()]


@app.post("/api/documents", status_code=201)
async def upload_document(file: UploadFile = File(...)) -> dict[str, object]:
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() not in {".txt", ".md"}:
        raise HTTPException(415, "当前仅支持 UTF-8 .txt 和 .md 文件")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件最大为 1 MB")
    try:
        content = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "请使用 UTF-8 编码的文本文件") from exc
    if len(content) < 20:
        raise HTTPException(400, "文档内容至少需要 20 个字符")
    title = Path(filename).stem[:120]
    with connect() as conn:
        if conn.execute("SELECT 1 FROM documents WHERE title=?", (title,)).fetchone():
            raise HTTPException(409, "同名文档已存在")
        doc_id = conn.execute("INSERT INTO documents(title, meta, created_at) VALUES (?, ?, ?)", (title, "本地上传 · UTF-8", now_iso())).lastrowid
        add_chunks(conn, doc_id, content)
        count = conn.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?", (doc_id,)).fetchone()[0]
        log_event(conn, f"知识库新增文档：{title}", f"已建立 {count} 个文本片段", "info")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / f"{doc_id}.txt").write_text(content, encoding="utf-8")
    return {"id": doc_id, "title": title, "chunks": count}


@app.get("/api/audit")
def list_audit() -> list[dict[str, object]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100").fetchall()]


@app.post("/api/diagnose")
def diagnose(body: DiagnosisRequest) -> dict[str, object]:
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "问题不能为空")
    with connect() as conn:
        devices = [dict(row) for row in conn.execute("SELECT * FROM devices").fetchall()]
        alerts = [dict(row) for row in conn.execute("SELECT * FROM alerts WHERE state != '已恢复'").fetchall()]
        references = search_runbooks(conn, question)
        context = json.dumps({"devices": devices, "active_alerts": alerts}, ensure_ascii=False)
        result = model_diagnosis(question, context, references) if MODEL_API_KEY else demo_diagnosis(question, context, references)
        conn.execute("INSERT INTO diagnoses(question, mode, result_json, created_at) VALUES (?, ?, ?, ?)", (question, result["mode"], json.dumps(result, ensure_ascii=False), now_iso()))
        log_event(conn, "AI 完成设备故障诊断", f"问题：{question[:160]} · 模式：{result['mode']}", "info")
    return result


@app.get("/api/diagnoses")
def list_diagnoses() -> list[dict[str, object]]:
    with connect() as conn:
        return [{"id": row["id"], "question": row["question"], "mode": row["mode"], "created_at": row["created_at"]} for row in conn.execute("SELECT * FROM diagnoses ORDER BY id DESC LIMIT 50")]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")
