# Action6 八项审计依据

源码基线为 `509002c25308eda98e0096068d3510485938027e`。本记录是测试侧交接证据，不改变业务规格。完整节点由仓库 `selection.json` 的 action6 项登记，本次没有删除节点、参数或断言。

前七项组合安装 wheel、真实客户端 Application/Files、C host、CLI、SQLite、文件交接及真实 ffprobe；相机替身只位于受约束的 ADB 进程边界。最后一项只组合客户端导出、安装 CLI、配置与 SQLite，不执行拍摄。

| 原参数或节点末尾 | 要保护的业务要求和可发现错误 |
| --- | --- |
| reached-copy-progress | 目标60秒、18+27+22=67秒。设备真实读取已流出且部分进度已提交后终止旧执行组；按原copy、文件、轮次、累计预算与已提交偏移续读。发现从零重拷、重置预算或换身份。 |
| reached-inspection-saved | 首段检查结果可靠提交后终止并重开host；前三段各检查一次，发现已提交结果丢失或重复探测。 |
| reached-source-commit-receipt | 来源共同提交后回执返回前中断；原两名等待者复用检查副本，提交后新等待者读设备原片。发现重算提交截点或把后到请求加入旧集合。 |
| reached-local-read | 第一组3份交付已准备，第二组仍读检查源；检查源保持REQUIRED/NOT_NEEDED并存在。终止重开host后完成两组交付再清理，发现过早删源。 |
| unknown-prefix | 第三段探测失败，前两段45秒，固定unconfirmed/probe_error范围；可靠控制完成允许来源成功。后段仍可明确ID取回。发现跳过未知段、补零、误判全部不足。 |
| complete-short-sequence | 完整3段18+20+12=50秒，来源failed，confirmed_short/invalid_outputs；两组仍各取得3份独立交付。发现控制成功覆盖内容不足或来源失败禁止取回。 |
| native-required-photo-failure | video_jpeg缺JPEG，来源failed；视频目标独立为floor(300/2)+1=151帧，前三段165帧已达标。两组各取3份；START一次，STOP零次。 |
| video-modes-freeze | 双相机、双模式、两类动作均受理；改配置后新8项用新模式，旧8项保持原执行定义。 |

独立预期来自测试输入的固定时长/帧数与业务公式，不使用生产选段结论计算预期；数据库仅用来关联实际output ID。每份媒体继续核对领取原字节、报告size/SHA-256、客户端delivery/output身份、verified和摘要；两组本地复用交付还核对全部独立delivery、检查源归属、完整长度及验证状态。媒体数量：通常6加晚期1；提交回执分区6加post-H 3加晚期1；unknown 4加晚期1；short和native各6。

顺序保持领取并导入报告、逐份媒体上传、最后导出并持久化累计ACK；ACK对应最终报告的report_id/to_wm。四个reached分区继续停止旧host、确认旧执行组退出、构造新HostDemo并重送原请求。模式矩阵改写能力文件后继续原来逐命令Application重建。

权威依据：公共分段规格“覆盖与范围决策”“动作结果与默认取回”“检查副本与等待取回的复用”“持久化、共同提交与恢复”“报告与客户端”；Action6特化“分段目标与主视频度量”“正常、异常与动作结果”；camera-capture的Action6原生有限延时最低格式合同。

两处已创建无参数watch_failures却未传入240秒主报告等待。该观察器只拒绝本次host/run确定错误、重试耗尽或host退出，不将预期capture=failed当失败。拟补failure参数，成功预算保留240秒。既有观察器最窄验证7项通过，详细命令日志和XML在wait-helper目录。

native基线本次因真实CLI在ResultMembersCommand返回state_db_error失败，240秒主报告等待最终超时。此失败不能作为成功运行成本。完整日志、命令、原源码、XML、资源预算和阶段计时在baseline目录及summary.json。
