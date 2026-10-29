"""媒体准备由正式受理、会话装配和拍摄处理器保存原 START、STOP。"""

from .media_retry_fixtures import media_pipeline


def test_media_preparation_preserves_confirmed_calls_and_source(media_pipeline):
    owned, _roots, source_id = media_pipeline
    assert owned.connection.execute(
        "SELECT r.kind,t.status FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id ORDER BY t.id"
    ).fetchall() == [(1, 2), (2, 2)]
    assert owned.connection.execute(
        "SELECT source_action_id,presence_state,completion_state FROM device_files WHERE id=?", (source_id,)
    ).fetchone() == (1, 2, 3)
    assert owned.connection.execute("SELECT activity_state FROM device_activities WHERE action_id=1").fetchone() == (3,)
