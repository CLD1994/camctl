# 二进制包由现有安装规则收集，源码包由 CMake 按实际路径复制。
if(CPACK_INSTALL_CMAKE_PROJECTS)
  return()
endif()
set(exclusions)
foreach(pattern IN LISTS CPACK_SOURCE_IGNORE_FILES)
  list(APPEND exclusions REGEX "${pattern}" EXCLUDE)
endforeach()
file(COPY "${CPACK_HOST_SOURCE_DIR}/" DESTINATION "${CMAKE_INSTALL_PREFIX}"
  USE_SOURCE_PERMISSIONS ${exclusions})
