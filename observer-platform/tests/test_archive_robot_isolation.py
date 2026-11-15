import json
from uuid import uuid4
import pytest
from collector.archive_upload import ArchiveUploadClient,ArchiveUploadConfig,ArchiveUploadError
from tests.test_archive_upload import FakeApi,PROJECT,ROBOT,PASSWORD


def test_same_day_robots_get_distinct_datasets_and_legacy_links_remain():
    api=FakeApi();a=str(uuid4());b=str(uuid4())
    first=ArchiveUploadClient(ArchiveUploadConfig('robot-a@fixture.test',PASSWORD,robot_id=a),opener=api)
    second=ArchiveUploadClient(ArchiveUploadConfig('robot-b@fixture.test',PASSWORD,robot_id=b),opener=api)
    # Exercise only the metadata API fixture; no files or production calls.
    first.login();second.login()
    one=first._dataset({'day':'2026-11-09','robotId':a})
    two=second._dataset({'day':'2026-11-09','robotId':b})
    assert one!=two
    assert api.datasets[one]['name']=='地库_2026-11-09'
    assert api.datasets[two]['name']=='地库_2026-11-09_'+b
    assert first._dataset({'day':'2026-11-09','robotId':a})==one
    assert second._dataset({'day':'2026-11-09','robotId':b})==two


def test_foreign_manifest_is_refused_before_login_or_remote_calls(tmp_path):
    foreign=str(uuid4());own=str(uuid4())
    segment=tmp_path/'sealed';segment.mkdir()
    manifest={'schemaVersion':1,'robotId':foreign,'day':'2026-11-09','sensor':'原相机前','label':'原相机前_10:00-10:01',
              'startedAt':'2026-11-09T10:00:00+08:00','endAt':'2026-11-09T10:01:00+08:00','files':['video.mkv']}
    path=segment/'archive.json';path.write_text(json.dumps(manifest));(segment/'video.mkv').write_bytes(b'old-robot-data')
    api=FakeApi();c=ArchiveUploadClient(ArchiveUploadConfig('robot@fixture.test',PASSWORD,robot_id=own),opener=api)
    with pytest.raises(ArchiveUploadError) as error:c.upload_segment(segment)
    assert error.value.code=='ARCHIVE_ROBOT_IDENTITY_MISMATCH'
    assert api.calls==[] and json.loads(path.read_text())['robotId']==foreign
    assert (segment/'video.mkv').read_bytes()==b'old-robot-data'
