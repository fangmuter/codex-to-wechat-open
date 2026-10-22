"""Run the confirmed-plan delivery flow with a fake, no-network transport."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daily_plan_delivery import DeliveryInbox, DeliveryLedger, DeliveryService


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, text, idempotency_key, now, **_kwargs):
        self.sent.append((idempotency_key, text))
        return type("Result", (), {"state": "ACKED_API"})()


def main():
    now = datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai"))
    target = (now.date() + timedelta(days=1)).isoformat()
    payload = json.loads(
        (Path(__file__).resolve().parent / "delivery_plan.example.json")
        .read_text(encoding="utf-8")
    )
    payload["plan_id"] = f"demo-plan-{target}"
    payload["target_date"] = target
    payload["created_at"] = now.isoformat()

    with tempfile.TemporaryDirectory(prefix="codex-to-wechat-demo-") as directory:
        root = Path(directory)
        inbox_dir = root / "delivery-inbox"
        inbox_dir.mkdir()
        file_path = inbox_dir / f"DAILY_PLAN_DELIVERY_{payload['plan_id']}.json"
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        ledger = DeliveryLedger(root / "delivery-ledger.db")
        transport = FakeTransport()
        try:
            service = DeliveryService(DeliveryInbox(inbox_dir), ledger, transport)
            first = service.deliver_tomorrow(now, target_date=target)
            replay = service.deliver_tomorrow(now, target_date=target)
            print(f"first_result={first.status}")
            print(f"replay_result={replay.status}")
            print(f"fake_transport_send_count={len(transport.sent)}")
            print("network_access=NO")
        finally:
            ledger.close()


if __name__ == "__main__":
    main()
