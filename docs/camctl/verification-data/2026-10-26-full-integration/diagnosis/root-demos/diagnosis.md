# 根演示真实失败只读诊断

固定组合 `9fbb734c`，全部结论来自本轮既有真实 C/CLI 日志与只读数据库。未另跑用例。

| 模块 | 实际失败 | 首因 |
|---|---:|---|
| real_camera_demo | 4 | 设备通信替身的stream响应仍含本机目录前缀，基准扫描正确拒绝，动作在START前失败。 |
| Action6 builtin | 1 | 独立准备已保存v2的设置历史，实际START合法使用v1；旧断言仍从START索取准备历史。 |

四个相机演示的状态均为capture_failed/baseline_read_failed，START流程与尝试数均为0。last_error保存stdout_sink_failed与“目录路径不属于原固定目录”。本机脚本确实find /tmp/.../camera-files/mnt/...；WorldTransport.run会删本机前缀，run_stream没有同样映射。修复设备通信替身应保持正式路径校验和全部真实业务断言，不通过关闭目录扫描或放宽目录范围绕过。当前60秒等待只观察成功，未检查已经可靠失败的动作，故最终表象是超时。

Action6当前库中PREPARE_CAPTURE(kind10)成功，control_returned/v2保存15条设置历史：光圈1801的e3未确认错误、unknown效果与stdout均保留；bitrate=2有setting_command_accepted。START(kind1)成功、control_returned/v1且仅recording_sent；STOP确认、capture最终成功和报告已完成。这符合operation-fields.md:52的独立准备责任、准备规格及驱动登记的条件性v2。测试应在原准备结果核对完整附属历史和重放，另核对START发送事实；只将版本2改成1会丢失后续设置历史断言，不能这样处理。版本断言之后的取回、claim、ack未执行，不计通过。

三方源码比较见source-comparison.json：通信替身、directory_scan、ADB driver、Action6契约在a775/272/组合一致；baseline的directory_scan选择原已存在。Action6测试仅相邻运行包装调用发生变化，失败的版本和calls断言原已存在。未发现这些首因由本次merge冲突解析引入；没有运行基线，不能声称两基线实际已失败。

逐世界数据库事实及原脚本见real-camera-worlds.json；Action6原准备/START/STOP/结果事实见action6-facts.json；模块身份、收场和日志摘要见summary.json。
