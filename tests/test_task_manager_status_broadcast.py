import asyncio

from src.web.task_manager import task_manager


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


def test_update_status_broadcasts_to_registered_websocket():
    async def run_test():
        task_uuid = "test-status-broadcast"
        websocket = FakeWebSocket()

        task_manager.set_loop(asyncio.get_running_loop())
        task_manager.register_websocket(task_uuid, websocket)

        try:
            task_manager.update_status(
                task_uuid,
                "completed",
                email="demo@example.com",
                email_service="tempmail",
            )

            await asyncio.sleep(0.05)

            assert websocket.messages, "expected a status message to be broadcast"
            assert websocket.messages[-1]["type"] == "status"
            assert websocket.messages[-1]["status"] == "completed"
            assert websocket.messages[-1]["email"] == "demo@example.com"
            assert websocket.messages[-1]["email_service"] == "tempmail"
        finally:
            task_manager.unregister_websocket(task_uuid, websocket)

    asyncio.run(run_test())
