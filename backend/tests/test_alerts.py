"""CloudWatch alarm -> SNS -> DebugOps: signature verification, topic allowlist, alarm -> incident bug -> run."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import func, select

from app.config import get_settings
from app.integrations import sns
from app.models import ActivityEvent, Bug, Job, Repository, WorkflowRun
from tests.conftest import new_client
from tests.fake_github import FakeGitHub, installation, repo

TOPIC = "arn:aws:sns:ap-south-1:123456789012:realify-prod-alarms"
CERT_URL = "https://sns.ap-south-1.amazonaws.com/SimpleNotificationService-abc.pem"
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
_cert = (x509.CertificateBuilder().subject_name(_name).issuer_name(_name).public_key(_key.public_key())
         .serial_number(1).not_valid_before(datetime.now(UTC) - timedelta(days=1))
         .not_valid_after(datetime.now(UTC) + timedelta(days=1)).sign(_key, hashes.SHA256()))
CERT_PEM = _cert.public_bytes(serialization.Encoding.PEM)


def signed(msg: dict, version="2", key=_key) -> dict:
    msg = {"MessageId": str(uuid.uuid4()), "TopicArn": TOPIC, "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
           "SignatureVersion": version, "SigningCertURL": CERT_URL, **msg}
    sig = key.sign(sns.canonical_string(msg), padding.PKCS1v15(), hashes.SHA1() if version == "1" else hashes.SHA256())
    return {**msg, "Signature": base64.b64encode(sig).decode()}


def alarm(name="realify-api-5xx", state="ALARM") -> str:
    return json.dumps({"AlarmName": name, "AlarmDescription": "5xx > 5% on checkout", "AWSAccountId": "123456789012",
                       "NewStateValue": state, "NewStateReason": "Threshold Crossed: 1 datapoint [12.0] > 5.0",
                       "StateChangeTime": "2026-11-05T10:00:00.000+0000", "Region": "Asia Pacific (Mumbai)",
                       "Trigger": {"MetricName": "HTTPCode_Target_5XX_Count", "Namespace": "AWS/ApplicationELB",
                                   "Dimensions": [{"name": "LoadBalancer", "value": "app/realify-api/abc"}]}})


@pytest.fixture
def sns_env(monkeypatch, db):
    monkeypatch.setattr(sns, "_fetch_cert", lambda url: CERT_PEM)
    monkeypatch.setattr(get_settings(), "sns_topic_arns", [TOPIC])
    return db


async def enabled_repo(db, patterns=("realify-api-*",)):
    from app.github.client import GitHubApp
    from app.github.sync import full_sync
    f = FakeGitHub()
    f.repos[100] = [repo(1, "realify/api")]
    await full_sync(db, GitHubApp(get_settings(), transport=f.transport()))
    r = (await db.execute(select(Repository))).scalar_one()
    r.enabled, r.settings = True, {"alarm_patterns": list(patterns)}
    await db.commit()
    return r


async def post(msg: dict):
    async with new_client() as c:
        return await c.post("/api/v1/aws/sns", content=json.dumps(msg), headers={"Content-Type": "text/plain"})


def test_signature_verification_v1_and_v2():
    for v in ("1", "2"):
        sns.verify(signed({"Type": "Notification", "Message": "x"}, version=v), fetch_cert=lambda u: CERT_PEM)
    bad = signed({"Type": "Notification", "Message": "x"})
    bad["Message"] = "tampered"
    with pytest.raises(sns.SNSVerificationError, match="invalid signature"):
        sns.verify(bad, fetch_cert=lambda u: CERT_PEM)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(sns.SNSVerificationError, match="invalid signature"):
        sns.verify(signed({"Type": "Notification", "Message": "x"}, key=other_key), fetch_cert=lambda u: CERT_PEM)
    for url in ("http://sns.ap-south-1.amazonaws.com/x.pem", "https://evil.example/x.pem", "https://sns.ap-south-1.amazonaws.com.evil.io/x.pem"):
        with pytest.raises(sns.SNSVerificationError, match="not from an SNS endpoint"):
            sns.verify({**signed({"Type": "Notification", "Message": "x"}), "SigningCertURL": url}, fetch_cert=lambda u: CERT_PEM)
    old = signed({"Type": "Notification", "Message": "x"}, )
    with pytest.raises(sns.SNSVerificationError, match="too old"):
        sns.verify(old, now=datetime.now(UTC) + timedelta(hours=2), fetch_cert=lambda u: CERT_PEM)


async def test_endpoint_rejects_unverified_and_foreign_topics(sns_env):
    msg = signed({"Type": "Notification", "Message": alarm()})
    msg["Signature"] = base64.b64encode(b"forged").decode()
    assert (await post(msg)).status_code == 401
    assert (await post(signed({"Type": "Notification", "Message": alarm(), "TopicArn": "arn:aws:sns:ap-south-1:999:other"}))).status_code == 403


async def test_endpoint_disabled_without_allowlist(monkeypatch, db):
    monkeypatch.setattr(get_settings(), "sns_topic_arns", [])
    assert (await post(signed({"Type": "Notification", "Message": alarm()}))).status_code == 404


async def test_subscription_confirmation(sns_env, monkeypatch):
    import httpx
    from app.api import alerts
    seen = []

    class Client(httpx.AsyncClient):
        def __init__(self, **kw):  # only the SNS confirm call has no transport; the test's own API client keeps its ASGI one
            kw.setdefault("transport", httpx.MockTransport(lambda req: seen.append(str(req.url)) or httpx.Response(200)))
            super().__init__(**kw)
    monkeypatch.setattr(alerts.httpx, "AsyncClient", Client)
    url = "https://sns.ap-south-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=x&Token=t"
    r = await post(signed({"Type": "SubscriptionConfirmation", "Message": "confirm", "Token": "t", "SubscribeURL": url}))
    assert r.json() == {"status": "subscribed"} and seen == [url]
    evil = await post(signed({"Type": "SubscriptionConfirmation", "Message": "c", "Token": "t", "SubscribeURL": "https://evil.example/x"}))
    assert evil.status_code == 400 and len(seen) == 1  # never calls a non-SNS URL


async def test_alarm_creates_incident_and_starts_run_once(sns_env):
    db = sns_env
    await enabled_repo(db)
    first = (await post(signed({"Type": "Notification", "Message": alarm()}))).json()
    assert first["status"] == "processed" and first["run_id"]
    bug = await db.get(Bug, first["bug_id"])
    assert bug.source == "aws_alarm" and bug.data["labels"] == ["incident"] and "5xx > 5%" in bug.description
    again = (await post(signed({"Type": "Notification", "Message": alarm()}))).json()  # alarm fires again
    assert again["bug_id"] == bug.id and again["run_id"] is None  # deduplicated: no second run
    await db.refresh(bug)
    assert bug.occurrences == 2
    assert await db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
    assert await db.scalar(select(func.count()).where(Job.kind == "workflow.run")) == 1


async def test_ok_state_unmatched_and_duplicate_messages(sns_env):
    db = sns_env
    await enabled_repo(db)
    assert (await post(signed({"Type": "Notification", "Message": alarm(state="OK")}))).json()["status"] == "ignored"
    unmatched = (await post(signed({"Type": "Notification", "Message": alarm(name="billing-service-latency")}))).json()
    assert unmatched["status"] == "ignored" and "no repository" in unmatched["reason"]
    msg = signed({"Type": "Notification", "Message": alarm()})
    assert (await post(msg)).json()["status"] == "processed"
    assert (await post(msg)).json() == {"status": "duplicate"}  # same MessageId redelivered
    warnings = (await db.execute(select(ActivityEvent.message).where(ActivityEvent.source == "alerts"))).scalars().all()
    assert any("matched no repository" in w for w in warnings)
