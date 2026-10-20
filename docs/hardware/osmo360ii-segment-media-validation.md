# OSMO 360 II 分段媒体验收

`validate_osmo_segments.py` 只读取已取回到本地的文件。它不连接相机，不调用 ADB，不进入 camctl 的拍摄或取回流程。验收人员先整理本页定义的 JSON 清单，再用 Python 3.11 调用工具；工具记录源文件身份、实际副本、视频流时长、音频、帧时间戳和逐流解码诊断。

分段登记、完成依据和默认交付范围由[录像分段产物设计规格](../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md)定义。本工具只读取外部清单，不读取数据库内部类型，也不从相机文件数量推断任务结果。

## 准备验收清单

清单应为 UTF-8 JSON，`manifest_version` 固定为 `1`。清单中的 `file_id` 标识本次验收内的一份源文件记录；已知时，`output_id` 保留 camctl 对正式原片或预览分配的独立身份。若历史记录没有正式 `output_id`，可以填 `null`，不必用本地验收标识冒充正式身份。源路径用于说明文件来源，不会被工具打开；工具只打开 `local_copy_path`。

下面的例子展示异常多录时的两份保留原片、一份默认交付原片，以及一个不进入正式产物集合的空尾段观察。`null` 源长度或摘要可以用于记录缺失证据，但工具会把源与副本身份标为“未知”；正式验收应填入可核实的长度、SHA-256 和证据引用。

```json
{
  "manifest_version": 1,
  "acceptance_id": "osmo-record-2026-10-21-round-01",
  "source_action_instance_id": "action-instance-123",
  "recording_kind": "ordinary_recording",
  "recording_class": "abnormal",
  "target_duration_ms": 12000,
  "target_basis_ref": "task-record.json#action-instance-123",
  "retained_scope_status": "complete",
  "retained_file_ids": ["osv-0034", "osv-0035"],
  "default_scope_status": "complete",
  "default_file_ids": ["osv-0034"],
  "files": [
    {
      "file_id": "osv-0034",
      "output_id": "output-original-0034",
      "role": "original",
      "file_status": "retained",
      "source_device_path": "CAM001/CAM_20261021_120000_0034_D.OSV",
      "source_size_bytes": null,
      "source_sha256": null,
      "source_evidence_ref": null,
      "local_copy_path": "media/CAM_20261021_120000_0034_D.OSV"
    },
    {
      "file_id": "osv-0035",
      "output_id": "output-original-0035",
      "role": "original",
      "file_status": "retained",
      "source_device_path": "CAM001/CAM_20261021_120000_0035_D.OSV",
      "source_size_bytes": null,
      "source_sha256": null,
      "source_evidence_ref": null,
      "local_copy_path": "media/CAM_20261021_120000_0035_D.OSV"
    },
    {
      "file_id": "empty-tail-0036",
      "output_id": null,
      "role": "original",
      "file_status": "observed_empty",
      "source_device_path": "CAM001/CAM_20261021_120000_0036_D.OSV",
      "source_size_bytes": 0,
      "source_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "source_evidence_ref": "camera-readback.json#empty-tail-0036",
      "local_copy_path": "media/CAM_20261021_120000_0036_D.OSV"
    }
  ]
}
```

`retained_file_ids` 只列非零、正式保留的 OSV 原片；它必须覆盖全部保留原片，或将 `retained_scope_status` 标为 `unknown`。`default_file_ids` 必须是该集合的子集；它表示默认交付范围，不得通过集合以外的原片补足时长。把某个范围标成 `unknown` 时，清单可以保留已观察到的成员，但工具不会将这些成员当成完整范围。范围状态为 `complete` 且文件列表为空时，工具按已确定的空范围比较；空列表配合 `unknown` 则表示范围未确定。

清单中的 `files` 可以同时列出原片、配对预览和空文件观察。每项都需要唯一 `file_id`；`role` 只能是 `original` 或 `preview`。预览需要以 `preview_of_output_id` 指向它配对的正式原片。空文件观察使用 `file_status: "observed_empty"`、`source_size_bytes: 0` 和 `output_id: null`。预览和空文件观察不进入原片时长范围。

| 字段 | 含义 |
| --- | --- |
| `acceptance_id` | 本次媒体验收的稳定标识。 |
| `source_action_instance_id` | 产生这些分段的原任务身份。若历史记录没有该身份，可以填 `null`；工具会保留身份未知状态，不要求伪造 ID。 |
| `recording_kind` | `ordinary_recording` 或 `native_timelapse`。 |
| `recording_class` | `normal` 或 `abnormal`，表示本次范围分类。 |
| `target_duration_ms`、`target_basis_ref` | 原任务目标时长及可追溯的独立来源。时长为正整数毫秒。 |
| `retained_scope_status`、`retained_file_ids` | 全部保留原片的完整性状态及其 `file_id` 集合。状态为 `complete` 或 `unknown`。 |
| `default_scope_status`、`default_file_ids` | 默认交付范围的完整性状态及其 `file_id` 集合。状态为 `complete` 或 `unknown`。 |
| `files[].output_id` | 已知时记录正式产物 ID；历史记录没有该 ID 时填 `null`。`file_id` 仍是清单内必需的独立文件标识。 |
| `files[].source_device_path` | 原设备目录中的文件路径，仅作为身份记录，不执行设备读取。 |
| `files[].source_size_bytes`、`source_sha256` | 从独立来源记录的源长度和 SHA-256；可暂填 `null`，但身份核对将保持未知。 |
| `files[].source_evidence_ref` | 指向来源长度和摘要依据的文档、记录或归档条目。 |
| `files[].local_copy_path` | 本地副本路径。相对路径以清单所在目录为基准，绝对路径也可使用。 |

源副本身份只有在源长度、源摘要和证据引用都存在，且实测本地副本的长度与 SHA-256 一致时才标为 `match`。摘要或长度不一致时，工具保留 `mismatch`；缺少来源依据或本地文件时保留 `unknown`。媒体时长描述本地副本；源副本身份未核实时，工具不会把本地副本时长提升为源文件的时长结论。

普通录像的 `target_duration_ms` 来自原任务。正常录像的保留范围应完整；当默认范围也标为完整时，它必须与全部保留原片相同。异常录像分别列出全部保留原片和默认范围，范围外原片只会进入 `all_retained` 的测量，不会进入 `default_delivery` 的测量。

原生延时需要以下附加字段，且每项依据必须来自实测媒体以外的任务或设备资料：

```json
{
  "recording_kind": "native_timelapse",
  "target_duration_ms": 300000,
  "target_basis_ref": "task-record.json#action-456-target",
  "sampling_interval_ms": 2000,
  "sampling_interval_basis_ref": "task-record.json#action-456-interval",
  "playback_frame_rate": "30/1",
  "frame_rate_basis_ref": "device-readback.json#playback-frame-rate",
  "sample_endpoint_rule": "start_included_end_excluded",
  "endpoint_rule_basis_ref": "sampling-contract.md#endpoints"
}
```

工具按这些独立事实推导预期帧数和播放时长。`start_included_end_excluded` 表示从采样起点开始、直到目标时刻之前计数，帧数为 `ceil(target / interval)`；`start_and_end_included` 表示采样起点和恰好落在目标时刻的端点都计入，帧数为 `floor(target / interval) + 1`。预期播放时长按预期帧数除以已核实的播放帧率计算。缺少任务目标、采样间隔、帧率或端点规则的独立依据时，工具报告预期未知，不用本次媒体的实际帧数填补。

## 运行工具

运行环境需要 Python 3.11、`ffprobe` 和 `ffmpeg`。工具只读取清单引用的本地副本；它不修改媒体文件。省略容限参数时采用规格默认值 100 ms；部署可以提供其他非负整数毫秒值，`0` 表示不允许短缺。

报告输出路径必须与输入清单和所有本地媒体副本分开。工具会拒绝覆盖这些输入，并使用独立临时文件写入报告。

```bash
python3.11 docs/hardware/validate_osmo_segments.py \
  --manifest /path/to/osmo-media-manifest.json \
  --shortfall-tolerance-ms 100 \
  --output /path/to/osmo-media-report.json
```

可省略 `--shortfall-tolerance-ms` 使用默认值，也可传入 `0` 或其他非负整数。负值、布尔值、浮点值及非整数输入不接受。报告会记录实际生效的 `shortfall_tolerance_ms`。

## 报告与判定

报告顶层保存 `acceptance_id`、`source_action_instance_id` 及其状态、任务目标、实际容限、独立预期依据、`ffprobe`/`ffmpeg` 版本、`files`、`scopes` 和 `findings`。每个文件记录源身份字段及本地副本的绝对路径、长度、SHA-256 和身份核对结果。原片探测结果分别列出容器格式与时长、每路主视频、实际音频、封面图流及其他流。

双路主视频按 ffprobe 流顺序编号为 `main_video_ordinal` 0 和 1；`attached_pic` 封面图单独记录，不计作主视频。工具对每路记录原始时长字段、`duration_ts × time_base` 精确值、帧数、首末帧时间戳、单段内最大帧间隔、容器与主视频时长差、完整解码的退出码及诊断。音频流单独探测和完整解码，但不加入主视频时长。

所有精确毫秒值都以字符串保存。字段 `*_ms_exact` 使用整数文本或 `分子/分母` 文本；`shortfall_ms` 也采用同一精确文本格式。普通录像的预期播放时长取原任务目标。实际视频时长优先从逐流 `duration_ts × time_base` 取得，缺少该值时使用 ffprobe 的逐流 `duration`；容器时长、音频时长、帧率和封面图均不替代主视频时长。

报告的 `scopes.all_retained` 与 `scopes.default_delivery` 各自汇总所列文件的两路主视频时长，并分别用同一预期时长和本次容限判断。短缺量按 `max(0, 预期时长 - 实测时长)` 计算；两路视频分别比较，任何一路超过容限都使该范围未达时长条件。容限不按分段或视频路数累加。段内帧时间戳摘要和段间重叠或缺帧是不同证据；报告将 `cross_segment_continuity` 保持为 `not_assessed`。`source_copy_identity_status` 和 `source_duration_status` 分别表示源／副本身份核对与源时长结论，解码诊断仍单独保留。

报告保留 `media.probe_stderr` 和每项 `decode_checks[].diagnostics` 原文。解码器即使以退出码 0 结束，只要输出诊断，诊断仍会出现在文件结果和 `findings` 中。媒体时长条件、源副本身份、探测状态和解码诊断是分开的事实；报告生成成功不表示媒体通过，工具退出码 0 也不清除诊断。清单无效时，工具把错误写到标准错误、不创建报告并返回非零；ffprobe/ffmpeg 版本探测失败或报告无法写出时也返回非零。媒体本身的不足、未知状态或解码问题仍写入证据报告。

本地合成的双视频样本只验证工具行为。该工具不会证明真实相机分段、画面质量、浏览器播放能力、段间无缺帧或无重叠。ARM 或实机验收仍须引用独立任务、设备、文件身份和媒体证据；原生延时在缺少采样端点或已核实帧率依据时应保留“无法核实”。
