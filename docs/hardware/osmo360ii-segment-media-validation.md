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
      "source_device_path": "CAM_001/CAM_20261021120000_0034_D.OSV",
      "source_size_bytes": null,
      "source_sha256": null,
      "source_evidence_ref": null,
      "source_locator_basis_ref": "camera-directory-listing.json#osv-0034-path",
      "local_copy_path": "media/CAM_20261021120000_0034_D.OSV"
    },
    {
      "file_id": "osv-0035",
      "output_id": "output-original-0035",
      "role": "original",
      "file_status": "retained",
      "source_device_path": "CAM_001/CAM_20261021120000_0035_D.OSV",
      "source_size_bytes": null,
      "source_sha256": null,
      "source_evidence_ref": null,
      "source_locator_basis_ref": "camera-directory-listing.json#osv-0035-path",
      "local_copy_path": "media/CAM_20261021120000_0035_D.OSV"
    },
    {
      "file_id": "empty-tail-0036",
      "output_id": null,
      "role": "original",
      "file_status": "observed_empty",
      "source_device_path": "CAM_001/CAM_20261021120000_0036_D.OSV",
      "source_size_bytes": 0,
      "source_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "source_evidence_ref": "camera-readback.json#empty-tail-0036",
      "local_copy_path": "media/CAM_20261021120000_0036_D.OSV"
    }
  ]
}
```

`retained_file_ids` 只列非零、正式保留的 OSV 原片；它必须覆盖全部保留原片，或将 `retained_scope_status` 标为 `unknown`。`default_file_ids` 必须是该集合的子集；它表示默认交付范围，不得通过集合以外的原片补足时长。把某个范围标成 `unknown` 时，清单可以保留已观察到的成员，但工具不会将这些成员当成完整范围。范围状态为 `complete` 且文件列表为空时，工具按已确定的空范围比较；空列表配合 `unknown` 则表示范围未确定。

`source_evidence_ref` 指向源文件长度和摘要依据；`source_locator_basis_ref` 指向设备路径依据。两者表达不同事实，同一份真实记录可以同时作为两类依据。只要 `source_device_path` 或 `source_locator_basis_ref` 缺失，工具就把源路径身份范围保留为未知，不会用本地摘要推断设备源身份。对于已提供路径依据的保留范围，同一规范化 `source_device_path` 不能重复登记，即使两项使用不同 `file_id`、`output_id`、本地副本或路径依据引用也会拒绝。设备路径不同的源文件即使字节摘要相同仍分别计入范围；工具不会按 SHA-256 去重。

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
| `files[].source_device_path` | 原设备目录中的文件路径，仅作为身份记录，不执行设备读取；需配合下方路径依据才能确认源定位。 |
| `files[].source_size_bytes`、`source_sha256` | 从独立来源记录的源长度和 SHA-256；可暂填 `null`，但身份核对将保持未知。 |
| `files[].source_evidence_ref` | 指向来源长度和摘要依据的文档、记录或归档条目，只证明字节身份依据。 |
| `files[].source_locator_basis_ref` | 指向设备路径依据的文档、目录记录或归档条目；缺失时源定位身份保持未知。 |
| `files[].local_copy_path` | 本地副本路径。相对路径以清单所在目录为基准，绝对路径也可使用。 |

`byte_identity_status` 只比较源长度、源摘要依据与本地副本的实测值；缺少字节依据时为 `unknown`，实测不一致时为 `mismatch`。`source_locator_status` 只表示设备路径和路径依据是否齐全。总 `identity_status` 只有字节身份匹配且源定位已知时才为 `match`；任一身份依据缺失时为 `unknown`，字节不一致时为 `mismatch`。媒体时长描述本地副本；总身份未核实时，工具不会把本地副本时长提升为源文件的时长结论。

普通录像的 `target_duration_ms` 来自原任务。正常录像的保留范围应完整；当默认范围也标为完整时，它必须与全部保留原片相同。异常录像分别列出全部保留原片和默认范围，范围外原片只会进入 `all_retained` 的测量，不会进入 `default_delivery` 的测量。

原生延时的帧数数量验收采用[采样数量规则](../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md#原生延时的采样数量验收)。工具从原任务的可靠目标 `T_ms`、采样间隔 `I_ms` 及其独立依据推导参考数量范围 `[ceil(T_ms/I_ms), floor(T_ms/I_ms)+1]`，并以范围下界 `B` 为基准。默认帧数短缺容差为 3 帧；每路最低合格数量为 `max(1, B-E)`。目标 300 秒、间隔 2 秒时参考范围为 150～151 帧，默认每路至少 147 帧。高于参考范围上界的实测帧数会记录，不因偏多单独失败。

帧数数量判断不要求先取得独立播放帧率或采样端点规则；缺少它们时，`expected_playback` 仍为 `unknown`，而目标和间隔依据完整的 `expected_sampling_quantity` 及采样数量范围可以独立核验。缺少目标、间隔或对应依据时，数量也保持 `unknown`。显式容差使用 CLI 参数 `--frame-count-shortfall-tolerance`；省略参数采用工具默认值，传入 `0` 合法，负数不接受。该容差只影响帧数数量，不修改任务目标、设备等待或时长容限。

当前工具推导单一预期播放时长时需要以下附加字段，且每项依据必须来自实测媒体以外的任务或设备资料：

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

工具按这些独立事实推导唯一的预期播放帧数和播放时长。`start_included_end_excluded` 表示从采样起点开始、直到目标时刻之前计数，帧数为 `ceil(target / interval)`；`start_and_end_included` 表示采样起点和恰好落在目标时刻的端点都计入，帧数为 `floor(target / interval) + 1`。预期播放时长按预期播放帧数除以已核实的播放帧率计算。缺少播放帧率或端点依据时，工具不会猜测端点或写入预期播放帧数、预期时长；独立的采样数量规则仍只使用任务目标与采样间隔。

## 运行工具

运行环境需要 Python 3.11、`ffprobe`、`ffmpeg` 和清单解析依赖 `ijson`。依赖只供这个离线验收工具使用，不进入 camctl 运行依赖。先在验收环境安装唯一权威依赖清单：

```bash
python3.11 -m pip install -r docs/hardware/requirements-osmo-media-validation.txt
```

工具只读取清单引用的本地副本；它不修改媒体文件。整体时长容限与采样帧数容差分别配置：省略 `--shortfall-tolerance-ms` 时采用默认 100 毫秒；省略 `--frame-count-shortfall-tolerance` 时采用默认 3 帧。两项都可以配置为 `0`；时长参数以毫秒计，帧数参数以帧计。

报告输出路径必须与输入清单和所有本地媒体副本分开。工具会拒绝覆盖这些输入，并使用独立临时文件写入报告。

```bash
python3.11 docs/hardware/validate_osmo_segments.py \
  --manifest /path/to/osmo-media-manifest.json \
  --shortfall-tolerance-ms 100 \
  --frame-count-shortfall-tolerance 3 \
  --batch-size 64 \
  --output /path/to/osmo-media-report.json
```

可分别省略两个容限参数使用各自默认值，也可传入 `0` 或其他非负整数。两个参数都拒绝负数和非整数；报告记录实际生效的 `shortfall_tolerance_ms` 与 `frame_count_shortfall_tolerance`。后者不按分段或视频路数重复应用。

`--batch-size` 必须是正整数，默认 `64`。工具用本地临时 SQLite 文件索引清单项、范围成员、逐文件结果和 findings，并按该批次大小读取索引及写出报告；SQLite 文件和诊断工件都在运行结束后清理。工具不会访问 camctl 业务数据库。ffprobe 帧时间戳逐行摘要，子进程 stderr 由独立读取线程写入临时工件，报告编码时再完整写回对应诊断字段。报告先写入输出目录中的临时文件，全部编码成功后才原子替换目标；校验或编码失败不会发布半份报告，也不会覆盖清单或媒体副本。报告记录 `processing_batch_size`，不同批次大小不改变验收结论。

## 报告与判定

报告使用 `report_schema_version: 2`，顶层保存 `acceptance_id`、`source_action_instance_id` 及其状态、任务目标、两项实际容限、独立预期依据、`ffprobe`/`ffmpeg` 版本、`files`、`scopes` 和 `findings`。清单仍使用 `manifest_version: 1`。每个文件记录源身份字段及本地副本的绝对路径、长度、SHA-256 和身份核对结果。原片探测结果分别列出容器格式与时长、每路主视频、实际音频、封面图流及其他流。

双路主视频按 ffprobe 流顺序编号为 `main_video_ordinal` 0 和 1；`attached_pic` 封面图单独记录，不计作主视频。工具对每路记录原始时长字段、`duration_ts × time_base` 精确值、帧数、首末帧时间戳、单段内最大帧间隔、容器与主视频时长差、完整解码的退出码及诊断。音频流单独探测和完整解码，但不加入主视频时长或采样帧数。

数量判断以成功完成 `ffprobe -count_frames` 后取得的 `files[].media.main_videos[].frame_count_assessment.reliable_frame_count` 作为可靠帧数。`declared_frame_count` 是媒体声明值，`frame_coverage.decoded_frame_timestamp_count` 只数有可解析时间戳的帧记录；两者都不替代可靠帧数。工具逐流保留三项观察；声明值或有时间戳帧数与可靠帧数不同时，`frame_count_observation_difference` finding 会记录差值，数量汇总仍使用可靠帧数。若 ffprobe 没有成功结束或没有可靠计数，数量保持未知，即使声明值或时间戳计数存在也不补位。

普通录像的非尾段实测时长按[设备分段契约](../superpowers/specs/2026-10-20-osmo360ii-record-segmentation-design.md#设备分段与命名契约)核对下界及参考范围；偏长记录与整体短缺、文件完整性及解码结果分别表达。

所有精确毫秒值都以字符串保存。字段 `*_ms_exact` 使用整数文本或 `分子/分母` 文本；`shortfall_ms` 也采用同一精确文本格式。普通录像的预期播放时长取原任务目标。实际视频时长优先从逐流 `duration_ts × time_base` 取得，缺少该值时使用 ffprobe 的逐流 `duration`；容器时长、音频时长、帧率和封面图均不替代主视频时长。

报告的 `scopes.all_retained` 与 `scopes.default_delivery` 分别核验完整保留范围和默认交付范围；一个范围外的文件不会补足另一个范围。每个范围独立累计两路主视频的全部成员时长和可靠帧数，每路只应用一次相应容限。时长比较写入 `local_copy_duration_comparison`，帧数比较写入 `local_copy_sampling_quantity_comparison`。帧数流结果保留可靠累计值、已计数成员数、所需成员数、短缺及高于参考上界的数量；范围不完整或该路有成员缺少可靠计数时，该路状态为 `unknown`，并保留已观察到的部分累计值。可靠范围完整且两路中某一路全部成员可靠计数后，若该路低于最低数量则该路为 `fail`；另一条未知流继续保持 `unknown`。

`source_sampling_quantity_status` 只有在该范围内源文件与本地副本身份全部匹配时才复用本地数量结论；源身份未知或不匹配时，源数量结论为 `unknown`，本地数量事实仍保留。身份不匹配继续作为独立 finding。采样数量达标只说明该范围两路累计帧数满足数量条件，不代表时长比较、解码、帧率、内容质量或段间连续性通过。实际播放时长、解码诊断及段间重叠或缺帧是独立证据；报告将 `cross_segment_continuity` 保持为 `not_assessed`。

报告保留 `media.probe_stderr` 和每项 `decode_checks[].diagnostics` 原文。解码器即使以退出码 0 结束，只要输出诊断，诊断仍会出现在文件结果和 `findings` 中。媒体时长条件、源副本身份、探测状态和解码诊断是分开的事实；报告生成成功不表示媒体通过，工具退出码 0 也不清除诊断。清单无效时，工具把错误写到标准错误、不创建报告并返回非零；ffprobe/ffmpeg 版本探测失败或报告无法写出时也返回非零。媒体本身的不足、未知状态或解码问题仍写入证据报告。

本地合成的双视频样本只验证工具行为。该工具不会证明真实相机分段、画面质量、浏览器播放能力、段间无缺帧或无重叠。ARM 或实机验收仍须引用独立任务、设备、文件身份和媒体证据；原生延时在缺少采样端点或已核实帧率依据时，预期播放时长保持“无法核实”，采样数量按其独立规则核对。
