import os
import json
import uuid
import datetime
import secrets
import httpx
from contextlib import asynccontextmanager
from typing import Annotated, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Header, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi import BackgroundTasks
from fastapi import Security, HTTPException, status, Depends
from fastapi.security import APIKeyHeader
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ConfigDict
from groq import AsyncGroq
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import Base, engine, init_db, get_db, async_session_maker, ExtractionRecord, AgentJobRecord, Organization

from tools import TOOL_DEFINITIONS, AVAILABLE_TOOLS

from collections import Counter

load_dotenv()

# ==============================================================================
# 1. CLIENT & LIFECYCLE
# ==============================================================================
api_key_env = os.getenv("GROQ_API_KEY")
groq_client: Optional[AsyncGroq] = AsyncGroq(api_key=api_key_env) if api_key_env else None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global groq_client
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("[WARNING] GROQ_API_KEY is not set!")
    groq_client = AsyncGroq(api_key=api_key)
    
    # Initialize the database schema
    await init_db()
    print("[INFO] Database & AI Engine Initialized.")
    yield
    print("[INFO] Shutting down...")

app = FastAPI(
    title="Solo Studio AI Engine",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

templates = Jinja2Templates(directory="templates")

@app.get("/", tags=["Dashboard"])
async def serve_dashboard(request: Request):
    """Serves the interactive client studio dashboard."""
    return templates.TemplateResponse(request=request, name="index.html")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================================================================
# 2. SCHEMAS
# ==============================================================================
class TextAnalyzeRequest(BaseModel):
    raw_text: str = Field(..., min_length=15)

class AIStructuredOutput(BaseModel):
    summary: str
    sentiment: str
    action_items: List[str]
    key_entities: List[str]

class StoredRecordResponse(AIStructuredOutput):
    id: int
    raw_text: str

    model_config = ConfigDict(from_attributes=True)

class JobCreationResponse(BaseModel):
    job_id: str
    status: str
    message: str

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    query: str
    result: Optional[str] = None
    tools_used: Optional[List[str]] = None
    created_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None

class ActionItemReport(BaseModel):
    task: str
    owner: str = "Unassigned"
    urgency: str = "Medium"  # High, Medium, Low

class AgencyReportResponse(BaseModel):
    org_name: str
    report_title: str
    generated_at: str
    total_records_analyzed: int
    executive_summary: str
    sentiment_distribution: dict[str, int]
    prioritized_actions: list[ActionItemReport]
    key_stakeholders_and_entities: list[str]
    markdown_deliverable: str

class TenantStatusResponse(BaseModel):
    client_name: str
    monthly_quota: int
    used_requests: int
    remaining_requests: int
    reset_cycle: str = "Monthly Rolling"

# ==============================================================================
# 3. AUTH
# ==============================================================================
EXPECTED_API_KEY = os.getenv("STUDIO_ACCESS_KEY", "dev_secret_key_123")

api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)

async def get_current_org(
    api_key: str = Security(api_key_header),
    session: AsyncSession = Depends(get_db)
) -> Organization:
    """Validates tenant API keys, isolates clients, and tracks monthly quotas."""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid secret access key."
        )

    # Master development key fallback for test suites & admin
    if api_key == os.getenv("API_SECRET_KEY", "dev_secret_key_123"):
        stmt = select(Organization).where(Organization.name == "Internal Dev")
        res = await session.execute(stmt)
        dev_org = res.scalar_one_or_none()
        if not dev_org:
            dev_org = Organization(
                name="Internal Dev",
                api_key="dev_secret_key_123",
                monthly_quota=999999
            )
            session.add(dev_org)
            await session.commit()
            await session.refresh(dev_org)
        return dev_org

    # Check client key
    stmt = select(Organization).where(Organization.api_key == api_key, Organization.is_active == True)
    result = await session.execute(stmt)
    org = result.scalar_one_or_none()

    if not org:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid secret access key."
        )

    if org.usage_count >= org.monthly_quota:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Monthly usage quota exceeded."
        )

    org.usage_count += 1
    await session.commit()
    return org

async def get_current_org_readonly(
    api_key: str = Security(api_key_header),
    session: AsyncSession = Depends(get_db)
) -> Organization:
    """Validates tenant API keys without incrementing monthly usage count."""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key header."
        )

    clean_key = api_key.strip().strip('"').strip("'")

    # Master dev key fallback
    if clean_key == os.getenv("API_SECRET_KEY", "dev_secret_key_123"):
        stmt = select(Organization).where(Organization.name == "Internal Dev")
        res = await session.execute(stmt)
        dev_org = res.scalar_one_or_none()
        if not dev_org:
            dev_org = Organization(
                name="Internal Dev",
                api_key="dev_secret_key_123",
                monthly_quota=999999,
                usage_count=0
            )
            session.add(dev_org)
            await session.commit()
            await session.refresh(dev_org)
        return dev_org

    # Check client key safely without requiring is_active or mutating count
    stmt = select(Organization).where(Organization.api_key == clean_key)
    result = await session.execute(stmt)
    org = result.scalar_one_or_none()

    if not org:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid secret access key."
        )

    return org

# ==============================================================================
# WEBHOOK DISPATCHER
# ==============================================================================

DISCORD_SLACK_WEBHOOK_URL = os.getenv("DISCORD_SLACK_WEBHOOK_URL", "")

async def dispatch_client_webhook(event_type: str, org_name: str, payload_summary: str, details: dict):
    """
    Dispatches asynchronous JSON alerts to Slack or Discord incoming webhooks.
    Gracefully no-ops if no webhook URL is configured.
    """
    if not DISCORD_SLACK_WEBHOOK_URL:
        print("[Webhook Notice] No DISCORD_SLACK_WEBHOOK_URL set. Skipping alert.")
        return

    # Details string formatted for readable display
    details_str = "\n".join([f"• **{k}**: {v}" for k, v in details.items()])
    
    formatted_msg = (
        f"🚀 **Solo AI Studio Alert** | `{org_name}`\n"
        f"**Event**: {event_type}\n"
        f"**Summary**: {payload_summary}\n"
        f"**Details**:\n{details_str}"
    )

    # Including both "content" (Discord) and "text" (Slack) ensures cross-platform compatibility
    message = {
        "content": formatted_msg,
        "text": formatted_msg
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(DISCORD_SLACK_WEBHOOK_URL, json=message)
            print(f"[Webhook Status] Response code: {resp.status_code}")
            if resp.status_code >= 400:
                print(f"[Webhook Error Response]: {resp.text}")
    except Exception as exc:
        print(f"[Webhook Warning] Dispatch failed: {exc}")
        

# ==============================================================================
# 4. ENDPOINTS
# ==============================================================================
@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "healthy", "engine": "ready"}

@app.post(
    "/api/v1/extract-intelligence",
    response_model=StoredRecordResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["AI Processing"]
)
async def extract_intelligence(
    payload: TextAnalyzeRequest,
    current_org: Annotated[Organization, Depends(get_current_org)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    if not groq_client or not groq_client.api_key:
        raise HTTPException(status_code=500, detail="Groq API Key not configured.")

    system_instruction = (
        "You are an expert data extraction engine. Analyze the provided text and "
        "output ONLY valid JSON adhering strictly to this schema:\n"
        "{\n"
        '  "summary": "string",\n'
        '  "sentiment": "Positive" | "Neutral" | "Negative",\n'
        '  "action_items": ["string"],\n'
        '  "key_entities": ["string"]\n'
        "}\n"
        "Return raw JSON only."
    )

    try:
        chat_completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": payload.raw_text},
            ],
            model="openai/gpt-oss-120b",
            temperature=0.1,
        )
        parsed = json.loads(chat_completion.choices[0].message.content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Engine Error: {str(e)}")

    # Persist the output directly to the database
    record = ExtractionRecord(
        raw_text=payload.raw_text,
        extracted_data=parsed,
        org_id=current_org.id
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    extracted = record.extracted_data or {}
    summary_text = extracted.get("summary", "")
    short_summary = summary_text[:150] + ("..." if len(summary_text) > 150 else "")

    await dispatch_client_webhook(
        event_type="New Intelligence Extracted",
        org_name=current_org.name,
        payload_summary=short_summary,
        details={
            "Sentiment": extracted.get("sentiment", "Neutral"),
            "Actions Extracted": len(extracted.get("action_items", [])),
            "Remaining Quota": max(0, current_org.monthly_quota - current_org.usage_count)
        }
    )

    return StoredRecordResponse(
        id=record.id,
        raw_text=record.raw_text,
        summary=record.extracted_data.get("summary", ""),
        sentiment=record.extracted_data.get("sentiment", "Neutral"),
        action_items=record.extracted_data.get("action_items", []),
        key_entities=record.extracted_data.get("key_entities", [])
    )

@app.get(
    "/api/v1/history",
    response_model=List[StoredRecordResponse],
    tags=["AI Processing"]
)
async def get_history(
    current_org: Annotated[Organization, Depends(get_current_org)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """Returns only the historical extractions belonging to the authenticated tenant."""
    # Internal dev org can see all records, clients see only their own
    if current_org.name == "Internal Dev":
        stmt = select(ExtractionRecord).order_by(ExtractionRecord.created_at.desc())
    else:
        stmt = (
            select(ExtractionRecord)
            .where(ExtractionRecord.org_id == current_org.id)
            .order_by(ExtractionRecord.created_at.desc())
        )
        
    result = await db.execute(stmt)
    records = result.scalars().all()
    
    return [
        StoredRecordResponse(
            id=r.id,
            raw_text=r.raw_text,
            summary=r.extracted_data.get("summary", ""),
            sentiment=r.extracted_data.get("sentiment", "Neutral"),
            action_items=r.extracted_data.get("action_items", []),
            key_entities=r.extracted_data.get("key_entities", [])
        )
        for r in records
    ]

class AgentQueryRequest(BaseModel):
    query: str = Field(..., min_length=5)

class AgentQueryResponse(BaseModel):
    answer: str
    tools_executed: List[str]

@app.post(
    "/api/v1/agent-query",
    response_model=AgentQueryResponse,
    tags=["Agentic Workflows"],
    summary="Execute multi-step agent query with dynamic tool use"
)
async def run_agent_query(
    payload: AgentQueryRequest,
    _auth: Annotated[str, Depends(get_current_org)]
):
    if not groq_client or not groq_client.api_key:
        raise HTTPException(status_code=500, detail="Groq API Key not configured.")

    tools_used = []

    system_prompt = (
        "You are an autonomous AI studio assistant with access to two tools:\n"
        "1. calculate_mrr_run_rate(mrr: float, churn_rate: float): SaaS ARR projections.\n"
        "2. lookup_studio_database(limit: int): Retrieves recent saved extractions.\n\n"
        "If you need to call a tool, respond with ONLY valid JSON:\n"
        '{"action": "call_tool", "tool_name": "<name>", "arguments": {<args>}}\n\n'
        "When you have collected all required information to answer the user query, respond with ONLY:\n"
        '{"action": "final_answer", "answer": "<your thorough final response>"}\n\n'
        "Always output strictly valid JSON."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload.query}
    ]

    max_steps = 5  # Guardrail against infinite agent loops

    try:
        for _ in range(max_steps):
            completion = await groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                temperature=0.1,
            )

            decision = json.loads(completion.choices[0].message.content)

            if decision.get("action") == "call_tool":
                tool_name = decision.get("tool_name")
                tool_args = decision.get("arguments", {})
                tools_used.append(tool_name)

                # Execute matching tool
                if tool_name in AVAILABLE_TOOLS:
                    tool_output = await AVAILABLE_TOOLS[tool_name](**tool_args)
                else:
                    tool_output = json.dumps({"error": f"Tool '{tool_name}' not found."})

                # Append assistant call and system feedback to memory
                messages.append({"role": "assistant", "content": json.dumps(decision)})
                messages.append({
                    "role": "user",
                    "content": f"Output from {tool_name}: {tool_output}. Continue until you can provide a final_answer."
                })
            elif decision.get("action") == "final_answer":
                return AgentQueryResponse(
                    answer=decision.get("answer", "Analysis complete."),
                    tools_executed=tools_used
                )
            else:
                # Direct response fallback
                return AgentQueryResponse(
                    answer=completion.choices[0].message.content,
                    tools_executed=tools_used
                )

        # Fallback if max iterations exceeded
        return AgentQueryResponse(
            answer="Max agent reasoning steps reached.",
            tools_executed=tools_used
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent Execution Error: {str(e)}")

class CreateTenantRequest(BaseModel):
    client_name: str
    monthly_quota: int = 500

class TenantResponse(BaseModel):
    org_id: str
    client_name: str
    api_key: str
    monthly_quota: int

@app.post("/api/v1/admin/tenants", response_model=TenantResponse, tags=["Admin"])
async def create_tenant(
    payload: CreateTenantRequest,
    admin_key: str = Security(api_key_header),
    session: AsyncSession = Depends(get_db)
):
    """Provisions a new client organization and generates their API secret key."""
    if admin_key != os.getenv("API_SECRET_KEY", "dev_secret_key_123"):
        raise HTTPException(status_code=401, detail="Invalid secret access key.")

    new_key = f"sk_live_{secrets.token_urlsafe(24)}"
    new_org = Organization(
        name=payload.client_name,
        api_key=new_key,
        monthly_quota=payload.monthly_quota
    )
    session.add(new_org)
    await session.commit()
    await session.refresh(new_org)

    return TenantResponse(
        org_id=new_org.id,
        client_name=new_org.name,
        api_key=new_org.api_key,
        monthly_quota=new_org.monthly_quota
    )

# ==============================================================================
# BACKGROUND WORKER RUNNER
# ==============================================================================
async def process_agent_job_background(job_id: str, query: str):
    """Executes the agentic reasoning loop in an isolated background context."""
    global groq_client
    if groq_client is None:
        groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

    async with async_session_maker() as session:
        stmt = select(AgentJobRecord).where(AgentJobRecord.id == job_id)
        res = await session.execute(stmt)
        job = res.scalar_one_or_none()
        if not job:
            return

        job.status = "running"
        await session.commit()

        tools_used = []
        system_prompt = (
            "You are an autonomous AI studio assistant with access to two tools:\n"
            "1. calculate_mrr_run_rate(mrr: float, churn_rate: float): SaaS ARR projections.\n"
            "2. lookup_studio_database(limit: int): Retrieves recent saved extractions.\n\n"
            "If you need to call a tool, respond with ONLY valid JSON:\n"
            '{"action": "call_tool", "tool_name": "<name>", "arguments": {<args>}}\n\n'
            "When you have collected all required information to answer the user query, respond with ONLY:\n"
            '{"action": "final_answer", "answer": "<your thorough final response>"}\n\n'
            "Always output strictly valid JSON."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query}
        ]

        try:
            for _ in range(5):
                completion = await groq_client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=messages,
                    temperature=0.1,
                )
                decision = json.loads(completion.choices[0].message.content)

                if decision.get("action") == "call_tool":
                    tool_name = decision.get("tool_name")
                    tool_args = decision.get("arguments", {})
                    tools_used.append(tool_name)

                    if tool_name in AVAILABLE_TOOLS:
                        tool_output = await AVAILABLE_TOOLS[tool_name](**tool_args)
                    else:
                        tool_output = json.dumps({"error": f"Tool '{tool_name}' not found."})

                    messages.append({"role": "assistant", "content": json.dumps(decision)})
                    messages.append({
                        "role": "user",
                        "content": f"Output from {tool_name}: {tool_output}. Continue until you can provide a final_answer."
                    })
                elif decision.get("action") == "final_answer":
                    job.result = decision.get("answer", "Complete.")
                    job.status = "completed"
                    break

            if job.status != "completed":
                job.result = "Max reasoning steps reached without final synthesis."
                job.status = "completed"

        except Exception as e:
            job.status = "failed"
            job.result = f"Error during processing: {str(e)}"

        job.tools_used = tools_used
        job.completed_at = datetime.datetime.now(datetime.timezone.utc)
        await session.commit()

@app.get(
    "/api/v1/reports/client-brief",
    response_model=AgencyReportResponse,
    tags=["Agency Reporting"]
)
async def generate_client_report(
    current_org: Annotated[Organization, Depends(get_current_org)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """
    Aggregates the tenant's extracted records and formats an executive-ready 
    agency digest with an Action Register and ready-to-paste Markdown.
    """
    # 1. Fetch tenant-scoped records
    if current_org.name == "Internal Dev":
        stmt = select(ExtractionRecord).order_by(ExtractionRecord.created_at.desc())
    else:
        stmt = (
            select(ExtractionRecord)
            .where(ExtractionRecord.org_id == current_org.id)
            .order_by(ExtractionRecord.created_at.desc())
        )
    
    result = await db.execute(stmt)
    records = result.scalars().all()

    if not records:
        raise HTTPException(
            status_code=404, 
            detail="No intelligence records found to generate a report."
        )

    # 2. Aggregate analytics across records
    sentiments = []
    all_actions = []
    all_entities = set()
    summaries = []

    for r in records:
        data = r.extracted_data or {}
        sentiments.append(data.get("sentiment", "Neutral"))
        summaries.append(data.get("summary", ""))
        for action in data.get("action_items", []):
            all_actions.append(
                ActionItemReport(
                    task=action,
                    owner="Operations Lead",
                    urgency="High" if any(w in action.lower() for w in ["deadline", "urgent", "budget", "asap"]) else "Medium"
                )
            )
        for entity in data.get("key_entities", []):
            all_entities.add(entity)

    sentiment_counts = dict(Counter(sentiments))
    exec_summary = " ".join(summaries[:3]) if summaries else "No summaries available."
    generated_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 3. Compile client-ready Markdown deliverable
    md_lines = [
        f"# Client Intelligence Brief: {current_org.name}",
        f"**Generated:** {generated_timestamp} | **Records Analyzed:** {len(records)}\n",
        "## Executive Summary",
        f"> {exec_summary}\n",
        "## Operational Sentiment",
    ]
    for s, count in sentiment_counts.items():
        md_lines.append(f"- **{s}**: {count} logs")

    md_lines.append("\n## Prioritized Action Register")
    for item in all_actions:
        md_lines.append(f"- [{item.urgency}] **{item.task}** (Owner: `{item.owner}`)")

    md_lines.append("\n## Identified Entities & Stakeholders")
    md_lines.append(", ".join(f"`{e}`" for e in sorted(all_entities)) if all_entities else "None")

    markdown_deliverable = "\n".join(md_lines)

    return AgencyReportResponse(
        org_name=current_org.name,
        report_title=f"Intelligence Audit - {current_org.name}",
        generated_at=generated_timestamp,
        total_records_analyzed=len(records),
        executive_summary=exec_summary,
        sentiment_distribution=sentiment_counts,
        prioritized_actions=all_actions,
        key_stakeholders_and_entities=sorted(list(all_entities)),
        markdown_deliverable=markdown_deliverable
    )

@app.get(
    "/reports/export",
    response_class=HTMLResponse,
    tags=["Agency Reporting"]
)
async def export_branded_report(
    request: Request,
    api_key: str,
    db: Annotated[AsyncSession, Depends(get_db)]
):
    """
    Renders an executive-ready, print/PDF-optimized HTML report for stakeholders.
    Passes ?api_key=... in the query param so browser links open directly.
    """
    # Authenticate via query param for browser-friendly links
    result = await db.execute(select(Organization).where(Organization.api_key == api_key))
    org = result.scalars().first()
    if not org:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    # Reuse reporting logic
    report_data = await generate_client_report(current_org=org, db=db)

    return templates.TemplateResponse(
        request=request,
        name="report.html",
        context={"report": report_data}
    )

@app.get(
    "/api/v1/tenant/usage",
    response_model=TenantStatusResponse,
    tags=["Multi-Tenant Governance"]
)
async def get_tenant_usage(
    current_org: Organization = Depends(get_current_org_readonly)
):
    """
    Returns real-time usage statistics and remaining quota for the authenticated tenant.
    """
    usage = getattr(current_org, "usage_count", 0)
    quota = getattr(current_org, "monthly_quota", 100)
    name = getattr(current_org, "name", "Client")

    remaining = max(0, quota - usage)
    return TenantStatusResponse(
        client_name=name,
        monthly_quota=quota,
        used_requests=usage,
        remaining_requests=remaining
    )

# ==============================================================================
# ASYNC JOB ENDPOINTS
# ==============================================================================
@app.post(
    "/api/v1/jobs/submit",
    response_model=JobCreationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Async Job Workflows"],
    summary="Dispatch a multi-step agent query to the background queue"
)
async def submit_agent_job(
    payload: AgentQueryRequest,
    background_tasks: BackgroundTasks,
    _auth: Annotated[str, Depends(get_current_org)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    job_id = str(uuid.uuid4())
    new_job = AgentJobRecord(id=job_id, query=payload.query, status="pending")
    db.add(new_job)
    await db.commit()

    background_tasks.add_task(process_agent_job_background, job_id, payload.query)

    return JobCreationResponse(
        job_id=job_id,
        status="pending",
        message="Agent job queued successfully."
    )

@app.get(
    "/api/v1/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["Async Job Workflows"],
    summary="Poll status and results of a background agent job"
)
async def get_job_status(
    job_id: str,
    _auth: Annotated[str, Depends(get_current_org)],
    db: Annotated[AsyncSession, Depends(get_db)]
):
    stmt = select(AgentJobRecord).where(AgentJobRecord.id == job_id)
    res = await db.execute(stmt)
    job = res.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail="Job ID not found.")

    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        query=job.query,
        result=job.result,
        tools_used=job.tools_used,
        created_at=job.created_at,
        completed_at=job.completed_at
    )