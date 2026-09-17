# 随组件交付的依赖

版本、上游来源与每个文件的 SHA-256 集中记录在 [manifest.cmake](manifest.cmake)。CMake 在配置阶段校验相应依赖文件，不在构建时联网下载。

- `yyjson`：JSON 解析；源码编入模块静态库，许可见 [LICENSE](yyjson/LICENSE)。
- `cmocka`：单元测试框架，仅在 `HOST_BUILD_TESTS=ON` 时编译，许可见 [COPYING](cmocka/COPYING)。保留测试所需的上游源码及头文件，Linux 构建定义在组件 CMake 中配置。

更新时从登记的上游版本取得原始文件，更新此清单，重新执行组件单元、集成和目标环境验证。依赖代码不承载本项目的业务规则。
