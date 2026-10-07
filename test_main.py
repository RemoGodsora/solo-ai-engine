import os
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from main import app, groq_client
from groq import AsyncGroq
from database import init_db

@pytest.mark.asyncio
async def test_async_agent_job_lifecycle():
    # 1. Initialize all database tables (including organizations)
    await init_db()

    # Ensure groq_client exists for the test process
    import main
    if main.groq_client is None:
        main.groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        headers = {"x-api-key": "dev_secret_key_123"}

        # Submit background job
        submit_res = await ac.post(
            "/api/v1/jobs/submit",
            json={"query": "Calculate ARR for MRR 5000 with 1% churn."},
            headers=headers
        )
        assert submit_res.status_code == 202
        job_id = submit_res.json()["job_id"]
        assert job_id is not None

        # 2. Poll until completed or failed (max 25 seconds)
        last_data = {}
        for _ in range(25):
            await asyncio.sleep(1)
            poll_res = await ac.get(f"/api/v1/jobs/{job_id}", headers=headers)
            assert poll_res.status_code == 200
            last_data = poll_res.json()
            if last_data.get("status") in ["completed", "failed"]:
                break

        assert last_data.get("status") == "completed", f"Job failed: {last_data}"
        assert "result" in last_data

@pytest.mark.asyncio
async def test_multi_tenant_isolation_and_quotas():
    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        admin_headers = {"x-api-key": "dev_secret_key_123"}

        # 1. Provision Tenant A (Quota: 2)
        res_a = await ac.post(
            "/api/v1/admin/tenants",
            json={"client_name": "Agency Alpha", "monthly_quota": 2},
            headers=admin_headers
        )
        assert res_a.status_code == 200
        org_a = res_a.json()
        headers_a = {"x-api-key": org_a["api_key"]}

        # 2. Provision Tenant B (Quota: 5)
        res_b = await ac.post(
            "/api/v1/admin/tenants",
            json={"client_name": "Agency Beta", "monthly_quota": 5},
            headers=admin_headers
        )
        assert res_b.status_code == 200
        org_b = res_b.json()
        headers_b = {"x-api-key": org_b["api_key"]}

        # 3. Test Invalid API Key Rejection
        bad_res = await ac.post(
            "/api/v1/extract-intelligence",
            json={"raw_text": "Client call notes: Need MVP launch by Friday."},
            headers={"x-api-key": "sk_invalid_bogus_key"}
        )
        assert bad_res.status_code == 401

        # 4. Tenant A makes Request #1 (Quota count -> 1)
        req1 = await ac.post(
            "/api/v1/extract-intelligence",
            json={"raw_text": "Alpha call: Budget is 15000, deadline Q4."},
            headers=headers_a
        )
        assert req1.status_code in [200, 201]

        # 5. Tenant A makes Request #2 (Quota count -> 2, Quota exhausted)
        req2 = await ac.post(
            "/api/v1/extract-intelligence",
            json={"raw_text": "Alpha call 2: Need contractor onboarded next week."},
            headers=headers_a
        )
        assert req2.status_code in [200, 201]

        # 6. Tenant A makes Request #3 -> Must fail with 429 Quota Exceeded
        req3 = await ac.post(
            "/api/v1/extract-intelligence",
            json={"raw_text": "Alpha call 3: This request should exceed quota."},
            headers=headers_a
        )
        assert req3.status_code == 429
        assert "quota exceeded" in req3.json()["detail"].lower()

        # 7. Tenant B makes Request #1 -> Must succeed independently (Isolated Quota)
        req_b1 = await ac.post(
            "/api/v1/extract-intelligence",
            json={"raw_text": "Beta call: Discussing design sprint."},
            headers=headers_b
        )
        assert req_b1.status_code in [200, 201]

@pytest.mark.asyncio
async def test_agency_reporting_formatting():
    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        admin_headers = {"x-api-key": "dev_secret_key_123"}

        # 1. Provision a dedicated agency tenant
        res_tenant = await ac.post(
            "/api/v1/admin/tenants",
            json={"client_name": "Apex Consulting", "monthly_quota": 10},
            headers=admin_headers
        )
        agency_key = res_tenant.json()["api_key"]
        agency_headers = {"x-api-key": agency_key}

        # 2. Ingest two unstructured project transcripts
        await ac.post(
            "/api/v1/extract-intelligence",
            json={"raw_text": "Meeting notes: Q4 budget target is $40,000. Launch website by November."},
            headers=agency_headers
        )
        await ac.post(
            "/api/v1/extract-intelligence",
            json={"raw_text": "Follow-up: Urgent blocker on API integrations with Stripe."},
            headers=agency_headers
        )

        # 3. Generate formatted client brief
        report_res = await ac.get("/api/v1/reports/client-brief", headers=agency_headers)
        assert report_res.status_code == 200
        data = report_res.json()

        # 4. Verify formatting output
        assert data["org_name"] == "Apex Consulting"
        assert data["total_records_analyzed"] == 2
        assert len(data["prioritized_actions"]) >= 1
        assert "## Prioritized Action Register" in data["markdown_deliverable"]
        assert "## Executive Summary" in data["markdown_deliverable"]