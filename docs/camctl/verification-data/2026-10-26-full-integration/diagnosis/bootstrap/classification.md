# bootstrap 完整失败分类

冻结源码：`9fbb734ca18e56934ba32942009e9ea46eccd089`。完整结果：417 passed、955 failed、33 errors、1 skipped；988 个失败/错误节点均唯一对应完整 traceback。

|首因分类|节点数|
|---|---:|
|旧业务数据库装配|138|
|旧流式测试路径映射|14|
|旧CaptureRuntime装配|370|
|旧context字段|62|
|异步接口未await|278|
|旧维护初始化或连接探针|9|
|旧构造接口|24|
|继承生产缺陷|2|
|旧会话工厂被超时遮蔽|34|
|报告先后要求待判|1|
|样例完整字典断言过时|1|
|安装测试支持依赖遗漏|26|
|本次合并接缝回归|2|
|旧报告后台观察装配|1|
|异步夹具动态请求|8|
|旧媒体私有字段|11|
|旧报告连接数量探针|2|
|上游诊断增加后的断言口径|1|
|测试占满默认线程池|1|
|旧context连接观察入口|2|
|context缺快照拥有者|1|

来源归属：982 项继承测试装配/API/私有观察或支持代码缺口；2 项继承生产 closing 未导入；2 项合并接缝 inspection 完整阶段缓存回归（已修）；1 项上游结构化诊断增加后的断言不适配；1 项报告与设备推进先后契约未决。未分类 0，未决 1。

这些是实际首个阻断点的分类，不能据此声称适配后的982项均会通过。未运行旧提交对照；继承与合并归属来自父提交源码、完整trace、会话日志与实际业务数据库。

生产 closing 的原始两份 camctl.log 已复制到本目录，见 confirmed-production-closing.json。inspection 修复提交 69c9857f，根合入 4e42c81c；见 ../inspection-unknown-resume/final-provenance.json。

唯一未决：test_directory_binding_runtime.py::test_report_configuration_error_stops_normal_and_restricted_session[False]。最终 configuration_error 已正确返回，但报告后台错误交付前设备已调用一次；需要确认契约是否要求首报告先完成才能推进设备。
