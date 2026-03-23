from concurrent.futures import ThreadPoolExecutor

from src.web.task_manager import task_manager


def test_record_batch_task_result_is_atomic_under_threads():
    batch_id = "batch-atomic-test"
    task_manager.init_batch(batch_id, 100)

    statuses = ["completed"] * 60 + ["failed"] * 40

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(lambda status: task_manager.record_batch_task_result(batch_id, status), statuses))

    snapshot = task_manager.get_batch_status(batch_id)

    assert snapshot is not None
    assert snapshot["completed"] == 100
    assert snapshot["success"] == 60
    assert snapshot["failed"] == 40
    assert snapshot["skipped"] == 0
