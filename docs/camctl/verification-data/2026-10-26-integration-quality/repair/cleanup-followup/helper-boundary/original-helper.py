def _cleanup_inspection_inputs(demo, scenario, source_id, failure):
    deployment = scenario["deployment"]
    purpose = int(enum_for("intermediate_files.purpose").RECORDING_INPUT)
    released = int(enum_for("intermediate_files.retention_state").RELEASABLE)
    completed = int(enum_for("intermediate_files.cleanup_state").COMPLETED)
    deadline = time.monotonic() + 180
    run_log = deployment.root / "cleanup-runs.jsonl"
    observation = {}

    def save_observation():
        (deployment.root / "inspection-cleanup-lifecycle.json").write_text(
            json.dumps(observation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )

    def rows():
        return query_state(
            deployment.state_db,
            "SELECT id,relative_path,retention_state,cleanup_state FROM intermediate_files "
            "WHERE owner_action_id=? AND purpose=? ORDER BY id",
            (source_id, purpose),
        )

    def finished(current):
        return current and all(row[2] == released and row[3] == completed for row in current)

    def cleanup_or_consumed():
        current = rows()
        if finished(current):
            return "completed", current
        runs = _records(run_log)
        if not runs or not current:
            return None
        run = runs[-1]
        remaining = {row[0] for row in current if row[3] != completed}
        checked = {}
        for event_id, body_json in query_state(
            deployment.state_db,
            "SELECT id,body_json FROM history_events WHERE id>? ORDER BY id",
            (run["history_horizon"],),
        ):
            for row in json.loads(body_json)["rows"]:
                if row["table"] == "runtime_state":
                    file_id = row["after"].get("values", {}).get("cleanup_cursor_file_id")
                    if file_id in remaining:
                        checked[file_id] = event_id
        if remaining and remaining <= checked.keys():
            observation.update(previous_run=run, checked_event_ids=checked,
                               files_before_next_run=current)
            return "consumed", current
        return None

    outcome, current = wait_for(
        cleanup_or_consumed, max(0, deadline - time.monotonic()),
        "Action6 检查副本未完成清理或本轮检查", failure=failure,
    )
    identities = [(row[0], row[1]) for row in current]
    observation.update(files_before_cleanup=current, budget_seconds=180)
    save_observation()
    if outcome == "consumed":
        # 历史 KEEP 已占本轮机会；用途结束不允许在同一 run 再试。
        previous_pid = observation["previous_run"]["pid"]
        normal_exit = wait_for(
            lambda: match.group(0) if (match := re.search(
                rf"^call=\d+ command=run pid={previous_pid} "
                r"result=success exit_kind=0 exit=0 needs_run=0\b.*$",
                demo.module_log(), re.MULTILINE,
            )) else None,
            max(0, deadline - time.monotonic()),
            "已检查的 Action6 副本等待当前正常 run 自然结束", failure=failure,
        )
        observation["previous_run_result"] = normal_exit
        save_observation()

        def previous_group_gone():
            try:
                os.killpg(previous_pid, 0)
            except ProcessLookupError:
                return True
            return None

        wait_for(
            previous_group_gone, max(0, deadline - time.monotonic()),
            "自然结束的 Action6 run 执行组尚未收场", failure=failure,
        )
        demo.stop()
        failure = demo.watch_failures()
        demo.start(env=scenario["environment"])
        next_run = wait_for(
            lambda: runs[-1] if (runs := _records(run_log))
            and runs[-1]["pid"] != previous_pid else None,
            max(0, deadline - time.monotonic()),
            "下一次正常 run 未从真实 C host 启动", failure=failure,
        )
        observation["next_run"] = next_run
        save_observation()

    retained = wait_for(
        lambda: current if finished(current := rows()) else None,
        max(0, deadline - time.monotonic()),
        "全部绑定取回终态后，Action6 检查副本仍未完成清理",
        failure=failure,
    )
    assert retained
    assert [(row[0], row[1]) for row in retained] == identities
    for _identity, relative_path, _retention, _cleanup in retained:
        assert not (deployment.staging / relative_path).exists()
    observation["files_after_cleanup"] = retained
    save_observation()
    return retained
