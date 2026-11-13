from __future__ import annotations

from channel_orchestrator.after_email_followup import (
    after_task_id_from_extended,
    dependency_block_reason,
    should_cancel_for_failed_email,
)


class _Notion:
    def __init__(self, status):
        self._status = status

    def get_task_status(self, task_id):
        return self._status


def test_after_task_id_from_extended():
    assert after_task_id_from_extended({"afterTaskId": " abc "}) == "abc"
    assert after_task_id_from_extended({"after_task_id": "x"}) == "x"
    assert after_task_id_from_extended({}) is None
    assert after_task_id_from_extended(None) is None


def test_dependency_waits_until_completed():
    assert dependency_block_reason(_Notion("Pending"), {"afterTaskId": "t1"})
    assert dependency_block_reason(_Notion("In Progress"), {"afterTaskId": "t1"})
    assert dependency_block_reason(_Notion("Completed"), {"afterTaskId": "t1"}) is None


def test_dependency_failed_cancels():
    reason = dependency_block_reason(_Notion("Failed"), {"afterTaskId": "t1"})
    assert reason and should_cancel_for_failed_email(reason)
    reason2 = dependency_block_reason(_Notion("Cancelled"), {"afterTaskId": "t1"})
    assert reason2 and should_cancel_for_failed_email(reason2)
    assert not should_cancel_for_failed_email("waiting for afterTaskId t1 (status=Pending)")
