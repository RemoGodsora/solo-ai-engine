import json
from typing import Any, Dict
from sqlalchemy import select
from database import async_session_maker, ExtractionRecord

# ==============================================================================
# 1. TOOL IMPLEMENTATIONS (Python code executed on the server)
# ==============================================================================

async def calculate_mrr_run_rate(mrr: float, churn_rate: float) -> str:
    """Calculates projected annual run rate adjusted for monthly churn."""
    monthly_retention = 1.0 - (churn_rate / 100.0)
    projected_arr = mrr * 12 * (monthly_retention ** 6)
    return json.dumps({
        "current_mrr": mrr,
        "churn_rate_percent": churn_rate,
        "raw_annual_run_rate": mrr * 12,
        "churn_adjusted_arr": round(projected_arr, 2)
    })

async def lookup_studio_database(limit: int = 5) -> str:
    """Queries recent structured extraction records from the studio database."""
    async with async_session_maker() as session:
        query = select(ExtractionRecord).order_by(ExtractionRecord.id.desc()).limit(limit)
        result = await session.execute(query)
        records = result.scalars().all()
        
        output = []
        for r in records:
            output.append({
                "id": r.id,
                "summary": r.summary,
                "sentiment": r.sentiment,
                "action_items": r.action_items,
            })
        return json.dumps(output)

# Map tool names to their async callable functions
AVAILABLE_TOOLS = {
    "calculate_mrr_run_rate": calculate_mrr_run_rate,
    "lookup_studio_database": lookup_studio_database,
}

# ==============================================================================
# 2. TOOL SCHEMAS (Passed to Groq / LLM so it knows how to call them)
# ==============================================================================

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "calculate_mrr_run_rate",
            "description": "Calculates SaaS annual run rate and churn-adjusted financial projections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mrr": {
                        "type": "number",
                        "description": "Monthly Recurring Revenue in USD"
                    },
                    "churn_rate": {
                        "type": "number",
                        "description": "Expected monthly customer churn percentage (e.g., 2.5 for 2.5%)"
                    }
                },
                "required": ["mrr", "churn_rate"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_studio_database",
            "description": "Retrieves recent intelligence analysis records saved in the studio database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent records to retrieve (default is 5)"
                    }
                },
                "required": []
            }
        }
    }
]