import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from pkb.config import settings
from pkb.store import get_client, list_documents

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="PKB - Personal Knowledge Base")

# 세션별 대화 히스토리 + 에이전트 (in-memory)
_sessions: dict[str, dict] = {}


def _get_session(session_id: str) -> dict:
    if session_id not in _sessions:
        from pkb.agent import create_agent

        _sessions[session_id] = {
            "agent": create_agent(),
            "history": [],
        }
    return _sessions[session_id]


def _check_same_origin(request: Request) -> None:
    """악성 사이트에서의 cross-origin POST(CSRF) 차단.

    브라우저가 Origin 헤더를 항상 보내지는 않으므로, 존재할 때만 검사하고
    Origin의 호스트가 요청 호스트와 다르면 거부한다. curl/서버간 호출은
    Origin이 없어 통과한다 (127.0.0.1 바인딩으로 외부 차단됨)."""
    origin = request.headers.get("origin")
    if not origin:
        return
    try:
        origin_host = urlparse(origin).netloc
    except ValueError:
        raise HTTPException(status_code=403, detail="invalid origin")

    request_host = request.headers.get("host", "")
    if origin_host != request_host:
        raise HTTPException(status_code=403, detail="cross-origin request denied")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    es = get_client()
    docs = list_documents(es)
    return templates.TemplateResponse(
        request, "index.html", context={"docs": docs}
    )


@app.get("/query", response_class=HTMLResponse)
def query_page(
    request: Request,
    q: str = Query(""),
    category: str = Query(""),
    top_k: int = Query(settings.default_top_k),
):
    results = []
    if q:
        from pkb.retrieve import hybrid_search

        es = get_client()
        results = hybrid_search(
            es, q, category=category or None, top_k=top_k
        )

    return templates.TemplateResponse(
        request,
        "query.html",
        context={
            "q": q,
            "category": category,
            "top_k": top_k,
            "results": results,
        },
    )


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    session_id = str(uuid.uuid4())
    return templates.TemplateResponse(
        request,
        "chat.html",
        context={"session_id": session_id},
    )


@app.post("/chat")
def chat_submit(
    request: Request,
    message: str = Form(...),
    session_id: str = Form(...),
):
    _check_same_origin(request)
    from pkb.agent import chat

    session = _get_session(session_id)
    response, session["history"] = chat(
        session["agent"], message, session["history"]
    )
    return JSONResponse({"response": response})
