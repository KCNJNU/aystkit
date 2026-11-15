import sqlite3

from cloud.backend.test_issues import TestIssues as IssueStore


def test_issue_handoff_is_shared_across_accounts_and_carries_unresolved_items():
    now = [1793404800.0]
    db = sqlite3.connect(':memory:')
    issues = IssueStore(db, 'robot', clock=lambda: now[0])

    item = issues.add({'description': '后相机画面中断', 'category': '相机'}, actor='alice')
    assert issues.list(actor='bob')['today_issues'][0]['created_by'] == 'alice'

    now[0] += 86400
    handoff = issues.list(actor='bob')
    assert [row['id'] for row in handoff['pending_review']] == [item['id']]
    assert handoff['can_start'] is False

    issues.review(item['id'], {'status': 'unresolved'}, actor='bob')
    assert issues.list(actor='alice')['can_start'] is True

    now[0] += 86400
    assert issues.list(actor='alice')['pending_review'][0]['id'] == item['id']
    issues.review(item['id'], {'status': 'resolved'}, actor='alice')
    assert issues.list(actor='bob')['can_start'] is True


def test_issue_payload_validation_rejects_unknown_fields_and_control_characters():
    db = sqlite3.connect(':memory:')
    issues = IssueStore(db, 'robot')
    for payload in ({'description': 'x', 'unexpected': True},
                    {'description': 'x\u0000'}):
        try:
            issues.add(payload, actor='alice')
        except ValueError:
            pass
        else:
            raise AssertionError('invalid issue payload was accepted')
