"""Persistent, bounded fault history; shares the ingest checkpoint transaction."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from shared.faults import EVENT_CODE, KEY_WARNING_CODES, fault_group_key, should_record_fault
from .fault_catalog import (CATEGORIES, CATEGORY_IDS, describe_fault,
                            fault_category, fault_search_text)

RETENTION_DAYS = 30
MAX_RECORDS = 20_000
MAX_RECEIPTS = 100_000
MAX_OCCURRENCES = 100_000
MERGE_WINDOW_SECONDS = 600
LEVELS = ('INFO', 'WARN', 'ERROR', 'FATAL')
BEIJING = timezone(timedelta(hours=8))


def parse_filters(query, *, pagination=True, grouping=False, max_offset=MAX_RECORDS):
    allowed = {'level', 'code', 'category', 'q', 'start', 'end', 'format'}
    if grouping:
        allowed.add('grouped')
    if pagination:
        allowed |= {'limit', 'offset'}
    if any(key not in allowed or len(query.getall(key)) != 1 for key in query):
        raise ValueError('invalid log filters')
    result = {}
    if 'grouped' in query:
        if query['grouped'] != '1':
            raise ValueError('invalid log grouping')
        result['grouped'] = True
    for key, limit in (('level', 8), ('code', 64), ('category', 32), ('q', 256)):
        value = query.get(key, '').strip()
        if len(value) > limit:
            raise ValueError('log filter too long')
        if value:
            result[key] = value
    if result.get('category') not in (None, *CATEGORY_IDS):
        raise ValueError('invalid log category')
    if result.get('level') not in (None, *LEVELS):
        raise ValueError('invalid log level')
    if 'code' in result and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', result['code']):
        raise ValueError('invalid log code')
    for key in ('start', 'end'):
        if query.get(key):
            try:
                number = float(query[key])
            except (ValueError, OverflowError) as exc:
                raise ValueError('invalid log time') from exc
            if not math.isfinite(number) or not 0 <= number <= 253402300799:
                raise ValueError('invalid log time')
            result[key] = number
    if result.get('start', 0) > result.get('end', 253402300799):
        raise ValueError('invalid log time range')
    if pagination:
        for key, default, maximum in (('limit', 50, 200), ('offset', 0, max_offset)):
            value = query.get(key, str(default))
            if not re.fullmatch(r'[0-9]{1,6}', value):
                raise ValueError('invalid log pagination')
            result[key] = int(value)
            if not (1 if key == 'limit' else 0) <= result[key] <= maximum:
                raise ValueError('invalid log pagination')
    return result


def _selected_raw_where():
    # The input policy precedes grouping; operator filters are applied only to
    # complete incidents so pagination, first/last times and exports agree.
    codes = sorted(KEY_WARNING_CODES)
    placeholders = ','.join('?' for _ in codes)
    repeated_code = "CASE WHEN json_valid(details) THEN json_extract(details,'$.original_code') END"
    predicate = ("(level IN ('ERROR','FATAL') OR (level='WARN' AND (code IN (" + placeholders +
                 ") OR (code='REPEATED_FAULT_SUMMARY' AND " + repeated_code + " IN (" + placeholders + ")))))")
    return predicate, codes + codes


def _where(filters):
    parts, values = [], []
    for key in ('level', 'code'):
        if filters.get(key):
            parts.append(key + '=?')
            values.append(filters[key])
    if filters.get('category'):
        parts.append('fault_category(code,component,message,details)=?')
        values.append(filters['category'])
    if 'start' in filters:
        parts.append('last_timestamp>=?')
        values.append(filters['start'])
    if 'end' in filters:
        parts.append('timestamp<=?')
        values.append(filters['end'])
    if filters.get('q'):
        literal = filters['q'].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        columns = ('message','component','code','details',
                   'fault_search_text(code,component,message,details)')
        parts.append('(' + ' OR '.join(column + " LIKE ? ESCAPE '\\'" for column in columns) + ')')
        values += ['%' + literal + '%'] * len(columns)
    return (' WHERE ' + ' AND '.join(parts) if parts else ''), values


def _legacy_summary(code, details):
    if code != 'REPEATED_FAULT_SUMMARY':
        return {}
    try:
        value = json.loads(details)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _historical_code(code, details):
    original = _legacy_summary(code, details).get('original_code')
    return original if (isinstance(original, str) and EVENT_CODE.fullmatch(original)
                        and original != 'REPEATED_FAULT_SUMMARY') else code


def _historical_message(code, message, details):
    original = _legacy_summary(code, details).get('original_message')
    return original if isinstance(original, str) and original else message


def _historical_key(code, component, message, details):
    return json.dumps(fault_group_key(_historical_code(code, details), component,
                      _historical_message(code, message, details)), ensure_ascii=False, separators=(',', ':'))


def _historical_count(code, details, count):
    legacy = _legacy_summary(code, details)
    # Recent rows already store actual occurrences in count. Never add either
    # occurrences or suppressed_count again to an ordinary stored record.
    occurrences = legacy.get('occurrences')
    if type(occurrences) is int and 1 <= occurrences <= 1_000_000:
        return count
    suppressed = legacy.get('suppressed_count')
    if type(suppressed) is int and 1 <= suppressed <= 1_000_000:
        # Older merged summaries retain only the last batch's evidence. The
        # known lower bound is safer than inventing the missing batch sizes.
        return max(count, suppressed)
    return count


def _historical_count_lower_bound(code, details, count):
    if code != 'REPEATED_FAULT_SUMMARY' or count <= 1:
        return 0
    occurrences = _legacy_summary(code, details).get('occurrences')
    return int(not (type(occurrences) is int and 1 <= occurrences <= 1_000_000))


def _group_id(robot_id, source_id, level, code, component, message, details):
    # Component normally identifies the sensor. Direct device/topic identifiers
    # also protect callers that report multiple devices through one component.
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except (TypeError, ValueError):
            details = {}
    details = details if isinstance(details, dict) else {}
    device = {key: details[key] for key in ('topic', 'device', 'device_id', 'camera_id', 'hardware_id')
              if isinstance(details.get(key), str) and details[key]}
    if code == 'REPEATED_FAULT_SUMMARY':
        original = details.get('original_code')
        if isinstance(original, str) and EVENT_CODE.fullmatch(original) and original != code:
            code = original
        original_message = details.get('original_message')
        if isinstance(original_message, str) and original_message:
            message = original_message
    key = [robot_id, source_id, level, *fault_group_key(code, component, message), device]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _register_history_functions(db):
    # Export uses a separate read-only connection. Registration does not mutate
    # its database and the returned result remains a streaming sqlite cursor.
    for name, arguments, function in (
        ('fault_group_id', 7, _group_id),
        ('fault_category', 4, fault_category),
        ('fault_search_text', 4, fault_search_text),
        ('fault_history_key', 4, _historical_key),
        ('fault_history_code', 2, _historical_code),
        ('fault_history_message', 3, _historical_message),
        ('fault_history_count', 3, _historical_count),
        ('fault_history_count_lower_bound', 3, _historical_count_lower_bound),
    ):
        db.create_function(name, arguments, function, deterministic=True)


def _incident_view():
    predicate, values = _selected_raw_where()
    # Only narrow identities, times and counts travel through the window sorts;
    # full evidence is joined once from the latest physical row per incident.
    sql = '''WITH history AS (
        SELECT records.rowid AS raw_rowid,robot_id,source_id,level,timestamp,last_timestamp,received_at,
          MIN(COALESCE(spans.first_received_at,received_at),received_at) AS first_received_at,
          fault_history_key(code,component,message,details) AS group_key,
          fault_history_count(code,details,count) AS occurrence_count,
          fault_history_count_lower_bound(code,details,count) AS count_lower_bound
        FROM fault_logs records LEFT JOIN fault_log_spans spans ON spans.id=records.id WHERE ''' + predicate + '''
      ), previous AS (
        SELECT *,MAX(received_at) OVER (
          PARTITION BY robot_id,source_id,level,group_key ORDER BY first_received_at,raw_rowid
          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_received
        FROM history
      ), segments AS (
        SELECT *,SUM(CASE WHEN previous_received IS NULL OR first_received_at-previous_received>? THEN 1 ELSE 0 END)
          OVER (PARTITION BY robot_id,source_id,level,group_key ORDER BY first_received_at,raw_rowid
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS segment
        FROM previous
      ), totals AS (
        SELECT robot_id,source_id,level,group_key,segment,MIN(raw_rowid) AS representative_rowid,
          MIN(timestamp) AS timestamp,MAX(last_timestamp) AS last_timestamp,MAX(received_at) AS received_at,
          SUM(occurrence_count) AS count,MAX(count_lower_bound) AS count_lower_bound
        FROM segments GROUP BY robot_id,source_id,level,group_key,segment
      ), latest AS (
        SELECT totals.*,MAX(segments.raw_rowid) AS latest_rowid
        FROM totals JOIN segments USING (robot_id,source_id,level,group_key,segment)
        WHERE segments.received_at=totals.received_at
        GROUP BY totals.representative_rowid
      ), incidents AS (
        SELECT representative.id,latest.robot_id,latest.source_id,latest.level,
          latest.timestamp,latest.last_timestamp,latest.received_at,latest.count,
          fault_history_code(evidence.code,evidence.details) AS code,evidence.component,
          fault_history_message(evidence.code,evidence.message,evidence.details) AS message,
          CASE WHEN latest.count_lower_bound THEN json_set(evidence.details,'$.legacy_count_is_lower_bound',json('true'))
               ELSE evidence.details END AS details
        FROM latest JOIN fault_logs representative ON representative.rowid=latest.representative_rowid
          JOIN fault_logs evidence ON evidence.rowid=latest.latest_rowid
      ) '''
    return sql, values + [MERGE_WINDOW_SECONDS]


def _group_history_view():
    """Retained source timestamps plus honest ranges for pre-upgrade history.

    A missing exact entry (legacy data or the bounded timestamp cap) contributes
    only its known parent range and remaining count. It never becomes invented
    individual timestamps. Time/text filters apply to entries before grouping.
    """
    predicate, values = _selected_raw_where()
    sql = """WITH retained AS (
        SELECT records.* FROM fault_logs records WHERE """ + predicate + """
      ), covered AS (
        SELECT record_id,SUM(count) AS count FROM fault_log_occurrences GROUP BY record_id
      ), entries AS NOT MATERIALIZED (
        SELECT events.id,events.record_id,events.robot_id,events.source_id,events.level,
          events.timestamp,events.timestamp AS last_timestamp,events.received_at,
          fault_history_code(events.code,events.details) AS code,events.component,
          fault_history_message(events.code,events.message,events.details) AS message,
          events.details,events.count,events.group_id,
          CASE WHEN events.count>1 THEN 'reported_summary' ELSE 'event' END AS kind
        FROM fault_log_occurrences events JOIN retained ON retained.id=events.record_id
        UNION ALL
        SELECT 'historical:'||retained.id,retained.id,retained.robot_id,retained.source_id,retained.level,
          retained.timestamp,retained.last_timestamp,retained.received_at,
          fault_history_code(retained.code,retained.details),retained.component,
          fault_history_message(retained.code,retained.message,retained.details),
          CASE WHEN fault_history_count_lower_bound(retained.code,retained.details,retained.count)
            THEN json_set(retained.details,'$.legacy_count_is_lower_bound',json('true')) ELSE retained.details END,
          fault_history_count(retained.code,retained.details,retained.count)-COALESCE(covered.count,0),
          fault_group_id(retained.robot_id,retained.source_id,retained.level,retained.code,
                         retained.component,retained.message,retained.details),
          'historical_summary'
        FROM retained LEFT JOIN covered ON retained.id=covered.record_id
        WHERE fault_history_count(retained.code,retained.details,retained.count)>COALESCE(covered.count,0)
      ) """
    return sql, values


def _grouped_view(filters):
    view, values = _group_history_view()
    where, filter_values = _where(filters)
    # Carry only aggregate values through the large grouping operation. Lookup
    # one latest evidence payload per group by its primary key afterwards.
    sql = view + """, matching AS NOT MATERIALIZED (SELECT * FROM entries""" + where + """),
      groups AS (
        SELECT group_id,MIN(timestamp) AS timestamp,MAX(last_timestamp) AS last_timestamp,
          MAX(received_at) AS received_at,SUM(count) AS count,
          MAX(CASE WHEN json_extract(details,'$.legacy_count_is_lower_bound')=1 THEN 1 ELSE 0 END) AS count_lower_bound,
          SUBSTR(MAX(printf('%030.9f',received_at)||id),31) AS latest_id
        FROM matching GROUP BY group_id
      ), latest AS (
        SELECT groups.*,COALESCE(events.level,legacy.level) AS level,
          COALESCE(events.code,legacy.code) AS evidence_code,
          COALESCE(events.component,legacy.component) AS component,
          COALESCE(events.message,legacy.message) AS evidence_message,
          COALESCE(events.details,legacy.details) AS evidence_details,
          COALESCE(events.source_id,legacy.source_id) AS source_id
        FROM groups LEFT JOIN fault_log_occurrences events ON events.id=groups.latest_id
          LEFT JOIN fault_logs legacy ON legacy.id=SUBSTR(groups.latest_id,12)
            AND groups.latest_id LIKE 'historical:%'
      ), grouped AS (
        SELECT group_id AS id,timestamp,last_timestamp,received_at,level,
          fault_history_code(evidence_code,evidence_details) AS code,component,
          fault_history_message(evidence_code,evidence_message,evidence_details) AS message,
          CASE WHEN count_lower_bound THEN json_set(evidence_details,'$.legacy_count_is_lower_bound',json('true'))
            ELSE evidence_details END AS details,count,source_id,group_id
        FROM latest
      ) """
    return sql, values + filter_values


class FaultLogs:
    def __init__(self, db, *, clock=time.time):
        self.db, self.clock = db, clock
        self.max_records, self.max_receipts = MAX_RECORDS, MAX_RECEIPTS
        self.max_occurrences = MAX_OCCURRENCES
        self._last_prune = 0
        db.executescript('''
            CREATE TABLE IF NOT EXISTS fault_logs (
              id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, source_id TEXT NOT NULL,
              timestamp REAL NOT NULL, last_timestamp REAL NOT NULL, received_at REAL NOT NULL,
              level TEXT NOT NULL, code TEXT NOT NULL, component TEXT NOT NULL,
              message TEXT NOT NULL, details TEXT NOT NULL, count INTEGER NOT NULL,
              fingerprint TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS fault_logs_time ON fault_logs(last_timestamp DESC);
            CREATE INDEX IF NOT EXISTS fault_logs_received ON fault_logs(received_at);
            CREATE INDEX IF NOT EXISTS fault_logs_fingerprint ON fault_logs(fingerprint,received_at DESC);
            CREATE INDEX IF NOT EXISTS fault_logs_level ON fault_logs(level,last_timestamp DESC);
            CREATE TABLE IF NOT EXISTS fault_receipts (
              robot_id TEXT NOT NULL, source_id TEXT NOT NULL, event_id TEXT NOT NULL,
              received_at REAL NOT NULL, PRIMARY KEY(robot_id,source_id,event_id));
            CREATE INDEX IF NOT EXISTS fault_receipts_received ON fault_receipts(received_at);
            CREATE TABLE IF NOT EXISTS fault_log_spans (
              id TEXT PRIMARY KEY, first_received_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS fault_log_occurrences (
              id TEXT PRIMARY KEY, record_id TEXT NOT NULL,
              robot_id TEXT NOT NULL, source_id TEXT NOT NULL, timestamp REAL NOT NULL,
              received_at REAL NOT NULL, level TEXT NOT NULL, code TEXT NOT NULL,
              component TEXT NOT NULL, message TEXT NOT NULL, details TEXT NOT NULL,
              count INTEGER NOT NULL, group_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS fault_occurrences_record ON fault_log_occurrences(record_id);
            CREATE INDEX IF NOT EXISTS fault_occurrences_received ON fault_log_occurrences(received_at);
            CREATE INDEX IF NOT EXISTS fault_occurrences_group ON fault_log_occurrences(group_id,timestamp DESC,id DESC);
        ''')
        _register_history_functions(db)
        with db:
            self.prune(force=True)

    def prune(self, force=False):
        now = self.clock()
        prune_spans = force or now - self._last_prune >= 60
        if prune_spans:
            cutoff = now - RETENTION_DAYS * 86400
            self.db.execute('DELETE FROM fault_logs WHERE received_at<?', (cutoff,))
            self.db.execute('DELETE FROM fault_receipts WHERE received_at<?', (cutoff,))
            self.db.execute('DELETE FROM fault_log_occurrences WHERE received_at<?', (cutoff,))
            self._last_prune = now
        # Hard caps apply immediately, even during a sustained fault burst.
        for table, maximum in (('fault_logs', self.max_records), ('fault_receipts', self.max_receipts),
                               ('fault_log_occurrences', self.max_occurrences)):
            count = self.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            if count > maximum:
                self.db.execute('DELETE FROM ' + table + ' WHERE rowid IN '
                                '(SELECT rowid FROM ' + table + ' ORDER BY received_at ASC,rowid ASC LIMIT ?)',
                                (count - maximum,))
                if table == 'fault_logs':
                    prune_spans = True
        if prune_spans:
            self.db.execute('DELETE FROM fault_log_occurrences WHERE NOT EXISTS '
                            '(SELECT 1 FROM fault_logs WHERE fault_logs.id=fault_log_occurrences.record_id)')
            self.db.execute('DELETE FROM fault_log_spans WHERE NOT EXISTS '
                            '(SELECT 1 FROM fault_logs WHERE fault_logs.id=fault_log_spans.id)')

    def add_batch(self, robot_id, source_id, events):
        """Caller owns the transaction, including its sequence checkpoint."""
        now, added = self.clock(), 0
        for event in events:
            inserted = self.db.execute('INSERT OR IGNORE INTO fault_receipts VALUES (?,?,?,?)',
                                      (robot_id, source_id, event['id'], now)).rowcount
            if not inserted:
                continue
            occurrence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, robot_id + '/' + source_id + '/' + event['id']))
            # Receipts have their own bounded cap: a replay can outlive a
            # receipt while its original timestamp/evidence is still retained.
            # Re-acknowledge it without incrementing the incident a second time.
            if (self.db.execute('SELECT 1 FROM fault_log_occurrences WHERE id=?', (occurrence_id,)).fetchone()
                    or self.db.execute('SELECT 1 FROM fault_logs WHERE id=?', (occurrence_id,)).fetchone()):
                continue
            added += 1
            # Acknowledging a filtered event is intentional: old collectors and
            # offline backlogs must not retry it forever. Receipt and source
            # sequence are still committed by the caller's one transaction.
            if not should_record_fault(event['level'], event['code'], event['details']):
                continue
            group_id = _group_id(robot_id, source_id, event['level'], event['code'],
                                 event['component'], event['message'], event['details'])
            # Canonical summaries share a UI group, but physical rows must keep
            # their original code: a normal payload has no original_code field.
            fingerprint = hashlib.sha256((group_id + '/' + event['code']).encode()).hexdigest()
            details = json.dumps(event['details'], ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            occurrences = event['details'].get('occurrences', 1)
            if type(occurrences) is not int or not 1 <= occurrences <= 1_000_000:
                occurrences = 1
            previous = self.db.execute('SELECT id,received_at FROM fault_logs WHERE fingerprint=? AND received_at>=? '
                                       'ORDER BY received_at DESC LIMIT 1', (fingerprint, now - MERGE_WINDOW_SECONDS)).fetchone()
            if previous:
                record_id = previous[0]
                # Freeze a server-observed interval start before its end moves.
                # For an old row without metadata this preserves every link
                # already established by its previous received_at, without
                # guessing from the source clock or rewriting past evidence.
                self.db.execute('INSERT OR IGNORE INTO fault_log_spans VALUES (?,?)', (previous[0], previous[1]))
                self.db.execute('UPDATE fault_logs SET timestamp=MIN(timestamp,?),last_timestamp=MAX(last_timestamp,?),'
                                'received_at=?,message=?,details=?,count=count+? WHERE id=?',
                                (event['timestamp'], event['timestamp'], now, event['message'], details, occurrences, previous[0]))
            else:
                # IDs are scoped to authenticated sources, not trusted as a global identity.
                record_id = str(uuid.uuid5(uuid.NAMESPACE_URL, robot_id + '/' + source_id + '/' + event['id']))
                self.db.execute('INSERT INTO fault_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                (record_id, robot_id, source_id, event['timestamp'], event['timestamp'], now,
                                 event['level'], event['code'], event['component'], event['message'], details, occurrences, fingerprint))
                self.db.execute('INSERT INTO fault_log_spans VALUES (?,?)', (record_id, now))
            self.db.execute('INSERT OR IGNORE INTO fault_log_occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (occurrence_id, record_id, robot_id, source_id, event['timestamp'], now,
                             event['level'], event['code'], event['component'], event['message'],
                             details, occurrences, group_id))
        self.prune()
        return added

    @staticmethod
    def _item(row):
        fields = ('id', 'timestamp', 'last_timestamp', 'received_at', 'level', 'code',
                  'component', 'message', 'details', 'count', 'source_id')
        value = dict(zip(fields, row))
        value['details'] = json.loads(value['details'])
        value.update(describe_fault(value['code'],value['component'],value['message'],value['details']))
        return value

    def select(self, filters, *, export=False, connection=None):
        db = connection if connection is not None else self.db
        if db is not self.db:
            _register_history_functions(db)
        view, values = _incident_view()
        where, filter_values = _where(filters)
        columns = 'id,timestamp,last_timestamp,received_at,level,code,component,message,details,count,source_id'
        sql = view + 'SELECT ' + columns + ' FROM incidents' + where + ' ORDER BY last_timestamp DESC,id DESC'
        sql += ' LIMIT ? OFFSET ?'
        values += filter_values + [self.max_records if export else filters.get('limit', 50),
                                   0 if export else filters.get('offset', 0)]
        return db.execute(sql, values)

    def query(self, filters):
        if filters.get('grouped'):
            return self.grouped_query(filters)
        view, values = _incident_view()
        where, filter_values = _where(filters)
        total = self.db.execute(view + 'SELECT COUNT(*) FROM incidents' + where, values + filter_values).fetchone()[0]
        return {'items': [self._item(row) for row in self.select(filters)], 'total': total,
                'limit': filters.get('limit', 50), 'offset': filters.get('offset', 0),
                'retention_days': RETENTION_DAYS, 'max_records': self.max_records}

    def summary(self, filters=None):
        if filters and filters.get('grouped'):
            view, values = _grouped_view(filters)
            relation = 'grouped'
        else:
            view, values = _incident_view()
            relation = 'incidents'
        # Group once so totals and all category chips describe the same read.
        rows = self.db.execute(view + """SELECT fault_category(code,component,message,details),
            COUNT(*),COALESCE(SUM(level IN ('ERROR','FATAL')),0),
            COALESCE(SUM(level='WARN'),0),MAX(last_timestamp)
            FROM """ + relation + """ GROUP BY fault_category(code,component,message,details)""", values).fetchall()
        indexed = {row[0]:row[1:] for row in rows}
        categories = [dict(id=key,label=label,total=indexed.get(key,(0,0,0,None))[0],
                           error=indexed.get(key,(0,0,0,None))[1],
                           warning=indexed.get(key,(0,0,0,None))[2]) for key,label in CATEGORIES]
        stamps = [row[4] for row in rows if row[4] is not None]
        return dict(total=sum(row['total'] for row in categories),
                    error=sum(row['error'] for row in categories),
                    warning=sum(row['warning'] for row in categories),
                    last_timestamp=max(stamps) if stamps else None,
                    categories=categories,retention_days=RETENTION_DAYS,max_records=self.max_records)


    def grouped_query(self, filters):
        view, values = _grouped_view(filters)
        columns = 'id,timestamp,last_timestamp,received_at,level,code,component,message,details,count,source_id,group_id'
        rows = self.db.execute(view + 'SELECT ' + columns + ',COUNT(*) OVER()' +
                               ' FROM grouped ORDER BY last_timestamp DESC,id DESC LIMIT ? OFFSET ?',
                               values + [filters.get('limit', 50), filters.get('offset', 0)]).fetchall()
        total = rows[0][12] if rows else (self.db.execute(
            view + 'SELECT COUNT(*) FROM grouped', values).fetchone()[0] if filters.get('offset',0) else 0)
        items = []
        for row in rows:
            item = self._item(row[:11])
            item['group_id'] = row[11]
            items.append(item)
        return dict(items=items,total=total,limit=filters.get('limit',50),offset=filters.get('offset',0),
                    grouped=True,retention_days=RETENTION_DAYS,max_records=self.max_records,
                    max_occurrences=self.max_occurrences)

    def timestamps(self, group_id, filters):
        if not re.fullmatch(r'[a-f0-9]{64}', group_id):
            raise ValueError('invalid fault group')
        view, values = _group_history_view()
        where, filter_values = _where(filters)
        where += (' AND ' if where else ' WHERE ') + 'group_id=?'
        values += filter_values + [group_id]
        total, occurrence_count = self.db.execute(view +
            'SELECT COUNT(*),COALESCE(SUM(count),0) FROM entries' + where, values).fetchone()
        if not total:
            return None
        rows = self.db.execute(view +
            'SELECT id,timestamp,last_timestamp,count,kind,received_at,message,details FROM entries' + where +
            ' ORDER BY last_timestamp DESC,id DESC LIMIT ? OFFSET ?',
            values + [filters.get('limit',50),filters.get('offset',0)]).fetchall()
        items = []
        for row in rows:
            item = dict(zip(('id','timestamp','last_timestamp','count','kind','received_at','message','details'),row))
            item['details'] = json.loads(item['details'])
            items.append(item)
        return dict(items=items,total=total,occurrence_count=occurrence_count,
                    limit=filters.get('limit',50),offset=filters.get('offset',0),group_id=group_id)


def beijing_time(value):
    try:
        return datetime.fromtimestamp(value, BEIJING).isoformat(timespec='milliseconds')
    except (ValueError, OverflowError, OSError):
        return str(value)


def csv_cell(value):
    text = str(value)
    # Quoting by the CSV writer does not stop spreadsheet formulas.
    if text.lstrip(' \r\n\t').startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


CSV_HEADER = ('发生时间（北京时间）', '最后发生时间（北京时间）', '接收时间（北京时间）',
              '发生时间戳（秒）', '最后发生时间戳（秒）', '等级', '异常代码', '来源组件',
              '内容', '次数', '采集源', '详情', '记录编号',
              '传感器与系统类别', '设备名称', '中文标题', '报错含义', '可能影响', '排查建议')


def csv_row(item):
    values = (beijing_time(item['timestamp']), beijing_time(item['last_timestamp']), beijing_time(item['received_at']),
              item['timestamp'], item['last_timestamp'], item['level'], item['code'], item['component'],
              item['message'], item['count'], item['source_id'],
              json.dumps(item['details'], ensure_ascii=False, separators=(',', ':')), item['id'])
    description = describe_fault(item['code'],item['component'],item['message'],item['details'])
    values += tuple(description[key] for key in ('category_label','device_label','title','meaning','impact','suggestion'))
    output = io.StringIO(newline='')
    csv.writer(output).writerow([csv_cell(value) for value in values])
    return output.getvalue().encode('utf-8')
