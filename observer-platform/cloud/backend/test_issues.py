"""Manual daily test issues and next-day review state.

Issues belong to the robot rather than to a viewer account: a fault recorded by
one tester is part of the handoff for the next tester.  Each issue keeps its
creator, and each daily check keeps the reviewer, so the handoff remains
auditable while the start gate is shared by all approved viewers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import time
import unicodedata
import uuid

BEIJING = timezone(timedelta(hours=8))


def issue_day(stamp: float) -> str:
    return datetime.fromtimestamp(float(stamp), BEIJING).date().isoformat()


def _text(value, *, field: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{field} must be text')
    value = value.strip()
    if required and not value:
        raise ValueError(f'{field} is required')
    if len(value) > maximum or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')
                                  for c in value):
        raise ValueError(f'invalid {field}')
    return value


def validate_issue(value: dict) -> dict:
    """Validate and normalize an issue creation payload."""
    if not isinstance(value, dict):
        raise ValueError('invalid test issue')
    allowed = {'description', 'category', 'severity'}
    if set(value) - allowed:
        raise ValueError('invalid test issue fields')
    description = _text(value.get('description'), field='description', maximum=2000, required=True)
    category = _text(value.get('category', '其他'), field='category', maximum=64) or '其他'
    severity = _text(value.get('severity', 'normal'), field='severity', maximum=16) or 'normal'
    if severity not in {'low', 'normal', 'high', 'critical'}:
        raise ValueError('invalid issue severity')
    return {'description': description, 'category': category, 'severity': severity}


def normalize_review(value: dict) -> dict:
    """Accept the API's status field and common checkbox aliases."""
    if not isinstance(value, dict) or set(value) - {'status', 'resolved', 'note'}:
        raise ValueError('invalid test issue review fields')
    status = value.get('status')
    if status is None and 'resolved' in value:
        resolved = value['resolved']
        if not isinstance(resolved, bool):
            raise ValueError('resolved must be boolean')
        status = 'resolved' if resolved else 'unresolved'
    aliases = {'yes': 'resolved', 'no': 'unresolved', 'fixed': 'resolved',
               'unfixed': 'unresolved', 'ok': 'resolved', 'fail': 'unresolved',
               'true': 'resolved', 'false': 'unresolved'}
    if isinstance(status, bool):
        status = 'resolved' if status else 'unresolved'
    if isinstance(status, str):
        status = aliases.get(status.strip().lower(), status.strip().lower())
    if status not in {'resolved', 'unresolved'}:
        raise ValueError('review status must be resolved or unresolved')
    note = _text(value.get('note', ''), field='note', maximum=1000)
    return {'status': status, 'note': note}


class TestIssues:
    def __init__(self, db: sqlite3.Connection, robot_id='robot', *, clock=time.time):
        self.db, self.robot_id, self.clock = db, robot_id, clock
        db.executescript('''
          CREATE TABLE IF NOT EXISTS test_issues (
            id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, created_by TEXT NOT NULL,
            description TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL,
            created_at REAL NOT NULL, issue_day TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS test_issues_owner_day
            ON test_issues(robot_id,created_by,issue_day,created_at DESC);
          CREATE TABLE IF NOT EXISTS test_issue_reviews (
            issue_id TEXT NOT NULL, robot_id TEXT NOT NULL, reviewer TEXT NOT NULL,
            review_day TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL,
            reviewed_at REAL NOT NULL,
            PRIMARY KEY(issue_id,review_day));
          CREATE INDEX IF NOT EXISTS test_issue_reviews_robot_day
            ON test_issue_reviews(robot_id,reviewer,review_day);
        ''')
        # Early development builds keyed reviews by reviewer as well.  A
        # review is a robot-wide daily acknowledgement, so migrate that shape
        # while retaining the most recent decision for each issue/day.
        columns = {row[1]: row[5] for row in db.execute('PRAGMA table_info(test_issue_reviews)')}
        if columns and columns.get('reviewer') == 2 and columns.get('review_day') == 3:
            # This is the old (issue_id, robot_id, reviewer, review_day) key;
            # the new table has (issue_id, review_day).
            pk_names = [row[1] for row in sorted(db.execute(
                'PRAGMA table_info(test_issue_reviews)').fetchall(), key=lambda row: row[5])
                        if row[5]]
            if pk_names == ['issue_id', 'reviewer', 'review_day']:
                with db:
                    db.execute('ALTER TABLE test_issue_reviews RENAME TO test_issue_reviews_old')
                    db.execute('''CREATE TABLE test_issue_reviews (
                      issue_id TEXT NOT NULL, robot_id TEXT NOT NULL, reviewer TEXT NOT NULL,
                      review_day TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL,
                      reviewed_at REAL NOT NULL, PRIMARY KEY(issue_id,review_day))''')
                    db.execute('''INSERT INTO test_issue_reviews
                      SELECT old.issue_id,old.robot_id,old.reviewer,old.review_day,
                             old.status,old.note,old.reviewed_at
                      FROM test_issue_reviews_old old
                      JOIN (SELECT issue_id,review_day,MAX(reviewed_at) reviewed_at
                            FROM test_issue_reviews_old GROUP BY issue_id,review_day) latest
                        ON latest.issue_id=old.issue_id AND latest.review_day=old.review_day
                       AND latest.reviewed_at=old.reviewed_at''')
                    db.execute('DROP TABLE test_issue_reviews_old')
                    db.execute('CREATE INDEX IF NOT EXISTS test_issue_reviews_robot_day '
                               'ON test_issue_reviews(robot_id,reviewer,review_day)')

    @staticmethod
    def _validate_actor(actor):
        return _text(actor, field='account', maximum=64, required=True)

    def add(self, payload, *, actor='observer'):
        actor = self._validate_actor(actor)
        value = validate_issue(payload)
        now = self.clock(); issue_id = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO test_issues VALUES (?,?,?,?,?,?,?,?)',
                            (issue_id, self.robot_id, actor, value['description'],
                             value['category'], value['severity'], now, issue_day(now)))
        return self.get(issue_id, actor=actor, read_all=True)

    def get(self, issue_id, *, actor='observer', read_all=False, connection=None):
        db = connection if connection is not None else self.db
        try:
            issue_id = str(uuid.UUID(str(issue_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError('invalid test issue id') from exc
        row = db.execute('SELECT id,created_by,description,category,severity,created_at,issue_day '
                         'FROM test_issues WHERE robot_id=? AND id=?',
                         (self.robot_id, issue_id)).fetchone()
        # Issues describe the robot and are visible to every approved account.
        if row is None:
            return None
        result = self._row(row)
        review = db.execute('SELECT status,note,review_day,reviewed_at,reviewer '
                            'FROM test_issue_reviews WHERE robot_id=? AND issue_id=? '
                            'ORDER BY review_day DESC,reviewed_at DESC LIMIT 1',
                            (self.robot_id, issue_id)).fetchone()
        result['last_review'] = self._review_row(review) if review else None
        return result

    @staticmethod
    def _row(row):
        return dict(zip(('id', 'created_by', 'description', 'category', 'severity',
                         'created_at', 'issue_day'), row))

    @staticmethod
    def _review_row(row):
        if row is None:
            return None
        return dict(zip(('status', 'note', 'review_day', 'reviewed_at', 'reviewer'), row))

    def _rows(self, *, actor, read_all, days=30, connection=None):
        db = connection if connection is not None else self.db
        scope = ''
        if days is None:
            date_clause, args = '', (self.robot_id,)
        else:
            days = max(1, min(365, int(days)))
            cutoff = (datetime.fromtimestamp(self.clock(), BEIJING).date() -
                      timedelta(days=days - 1)).isoformat()
            date_clause, args = ' AND i.issue_day>=?', (self.robot_id, cutoff)
        rows = db.execute('SELECT i.id,i.created_by,i.description,i.category,i.severity,'
                          'i.created_at,i.issue_day FROM test_issues i '
                          'WHERE i.robot_id=?' + date_clause + scope +
                          ' ORDER BY i.created_at DESC,i.id DESC', args).fetchall()
        result = []
        for row in rows:
            value = self._row(row)
            review = db.execute('SELECT status,note,review_day,reviewed_at,reviewer '
                                'FROM test_issue_reviews WHERE robot_id=? AND issue_id=? '
                                'ORDER BY review_day DESC,reviewed_at DESC LIMIT 1',
                                (self.robot_id, row[0])).fetchone()
            value['last_review'] = self._review_row(review) if review else None
            result.append(value)
        return result

    def pending(self, *, actor='observer', read_all=False, now=None, connection=None):
        """Return issues that must be reviewed before today's test can start.

        Every issue from yesterday requires today's check.  Older issues only
        carry over while their latest decision is unresolved or absent.
        """
        actor = self._validate_actor(actor)
        now = self.clock() if now is None else now
        today = issue_day(now)
        yesterday = (datetime.fromtimestamp(now, BEIJING).date() - timedelta(days=1)).isoformat()
        values = self._rows(actor=actor, read_all=read_all, days=None, connection=connection)
        pending = []
        for value in values:
            if value['issue_day'] >= today:
                continue
            review = value['last_review']
            if value['issue_day'] == yesterday:
                if not review or review['review_day'] != today:
                    value['needs_review'] = True; pending.append(value)
            # A check/cross decision satisfies today's handoff even when the
            # issue is still present; an unresolved decision is carried into
            # tomorrow's review list.
            elif not review or (review['status'] != 'resolved' and review['review_day'] != today):
                value['needs_review'] = True; pending.append(value)
        return pending

    def list(self, *, actor='observer', read_all=False, days=30, connection=None):
        actor = self._validate_actor(actor)
        values = self._rows(actor=actor, read_all=read_all, days=days, connection=connection)
        pending_rows = self.pending(actor=actor, read_all=read_all, connection=connection)
        pending_ids = {value['id'] for value in pending_rows}
        # A long-running unresolved issue must remain actionable even when the
        # caller asks for a short history window.
        visible_ids = {value['id'] for value in values}
        values.extend(value for value in pending_rows if value['id'] not in visible_ids)
        for value in values:
            value['needs_review'] = value['id'] in pending_ids
        now = self.clock()
        today = issue_day(now)
        yesterday = (datetime.fromtimestamp(now, BEIJING).date() - timedelta(days=1)).isoformat()
        return {'today': today,
                'previous_date': yesterday,
                'issues': values,
                'today_issues': [value for value in values if value['issue_day'] == today],
                'pending_review': [value for value in values if value['needs_review']],
                'can_start': not pending_ids}

    def review(self, issue_id, payload, *, actor='observer', read_all=False):
        actor = self._validate_actor(actor)
        value = normalize_review(payload)
        try:
            issue_id = str(uuid.UUID(str(issue_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError('invalid test issue id') from exc
        row = self.db.execute('SELECT created_by,issue_day FROM test_issues '
                              'WHERE robot_id=? AND id=?', (self.robot_id, issue_id)).fetchone()
        if row is None:
            return None
        now = self.clock(); today = issue_day(now)
        if row[1] >= today:
            raise ValueError('只能复核之前日期的问题')
        with self.db:
            self.db.execute('INSERT INTO test_issue_reviews VALUES (?,?,?,?,?,?,?) '
                            'ON CONFLICT(issue_id,review_day) DO UPDATE SET '
                            'status=excluded.status,note=excluded.note,reviewed_at=excluded.reviewed_at, '
                            'reviewer=excluded.reviewer',
                            (issue_id, self.robot_id, actor, today, value['status'], value['note'], now))
        return self.get(issue_id, actor=actor, read_all=True)

    def review_required(self, *, actor='observer', read_all=False, now=None):
        return self.pending(actor=actor, read_all=read_all, now=now)
