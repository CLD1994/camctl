# 视频大小估算共同样例

[完整能力说明](capabilities.json)包含虚构设备 `estimate_demo_cam0` 和虚构驱动 `fictional_estimate_camera`。全部数值用于软件验收，不表示真实相机的能力、有效档位或测量结果。

| 参数类型 | 估算依据与示例参数 |
| --- | --- |
| `example_record` | 按码率档位查表，直接读取成片时长。`high`、60 秒对应 130 Mbps、975 MB。 |
| `example_record_from` | 从显式参数读取采用 Mbps 的参考码率，直接读取以秒计的成片时长。 |
| `example_timelapse` | 按采集持续时间与间隔估算成片时长，使用固定播放帧率和码率。6000 秒、25 秒间隔对应 8 秒成片、175 MB。 |
| `example_timelapse_frames` | 预计成片帧数除以固定播放帧率。240 帧对应 8 秒成片、175 MB。 |

字段含义与整份失败规则由[能力说明格式](../../../docs/architecture/capabilities.md#参数类型的视频大小估算)定义；公式和完整状态分类见[视频大小估算设计](../../../docs/superpowers/specs/2026-10-10-video-size-estimate-design.md)。

## 原始文本与三层预期

[共同用例](cases.json)是数组，每项包含 `name`、`json`、`schema_valid`、`encode_valid` 和 `load_valid`。`json` 保存完整能力文档的原始 JSON 文本。消费方必须直接解析该文本，不能先用普通解析器读入再序列化，否则会丢失精确小数、指数写法和重复成员。

| 预期字段 | 判断范围与负责入口 |
| --- | --- |
| `schema_valid` | 普通 `JSON.parse` 后的外层能力 Schema 结构。`node scripts/check-protocol.mjs` 与 `node --test scripts/check-protocol.test.mjs` 只检查这层预期。 |
| `encode_valid` | Python 精确解析与整份编码检查，包括重复成员和估算数值的原始数学值。驱动 Catalog 的参数 Schema、类型关联和标识唯一性另由实际导出链检查，不能把任意外部文档的编码通过等同于 Catalog 导出通过。 |
| `load_valid` | 客户端精确解析、外层结构和全部加载语义，包括参数 Schema、类型关联、重复成员与各层标识唯一性。 |

例如，帧数 `240`、`240.0` 和 `2.4e2` 的数学值均为整数，三层预期均有效。`1.00000000000000000001` 会被普通浮点解析舍入为整数，因此结构检查通过，但精确编码与加载必须拒绝。重复对象成员也可能通过普通结构检查，实际编码与加载仍须拒绝。

同一作用域的重复设备、动作或参数类型标识可以通过外层结构及外部文档编码，但整份加载必须失败。同名参数类型出现在不同设备或不同动作下仍然有效。结构检查通过只证明共享文本符合对应外层结构预期，不证明生产组件已经完成加载或设备接入。
