"""Revocable accounts stay isolated when usernames are registered again."""
import pytest

from cloud.backend.security import Sessions, hash_password, verify_password


@pytest.mark.parametrize('password', ['x', '123', '长密码' * 300, 'x' * 2048])
def test_password_hash_and_verify_have_no_character_count_rule(password):
    encoded = hash_password(password)
    assert verify_password(password, encoded)
    assert not verify_password(password + 'wrong', encoded)


@pytest.mark.parametrize('password', ['', None, 123])
def test_password_remains_required_and_typed(password):
    with pytest.raises(ValueError):
        hash_password(password)
    assert verify_password(password, hash_password('fixture')) is False


def test_revoke_all_sessions_for_one_user_keeps_administrator_and_other_viewer():
    sessions = Sessions('isolated-session-secret' * 3)
    first = sessions.issue('member', account_id=1)
    second = sessions.issue('member', account_id=1)
    observer = sessions.issue('observer')
    other = sessions.issue('another', account_id=2)
    nonces = {sessions.read(cookie)['n'] for cookie in (first, second)}
    assert set(sessions.revoke_user('member')) == nonces
    assert sessions.read(first) is sessions.read(second) is None
    assert sessions.read(observer)['u'] == 'observer'
    assert sessions.read(other)['a'] == 2
    assert sessions.revoke_user('member') == []
    later = sessions.issue('member', account_id=3)
    assert sessions.read(later)['a'] == 3
    assert sessions.read(first) is None


def test_expired_and_evicted_session_owner_indexes_remain_bounded():
    now = [1.0]
    sessions = Sessions('isolated-session-secret' * 3, ttl=10, maximum=2, clock=lambda: now[0])
    first = sessions.issue('first', account_id=1)
    sessions.issue('second', account_id=2)
    sessions.issue('third', account_id=3)
    assert sessions.read(first) is None
    assert len(sessions.active) == len(sessions.usernames) == 2
    assert not sessions.revoke_user('first')
    now[0] += 11
    assert sessions.read(sessions.issue('fourth', account_id=4))['a'] == 4
    assert len(sessions.active) == len(sessions.usernames) == 1
